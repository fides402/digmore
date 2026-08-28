package com.digmore.newpipecli

import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder

/**
 * A YouTube search that does NOT go through NewPipeExtractor's own parser.
 * Ported from diggaplayer's NewPipeSearchFallback.kt (itself from jatz):
 * fetch the results page, pull out embedded `ytInitialData`, and find videos
 * by recursively walking the whole tree for any object with a `videoId` and
 * a title. Schema-agnostic, so a YouTube layout change can't NPE it.
 */
object SearchFallback {

    private const val USER_AGENT =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

    private val INITIAL_DATA = Regex(
        """(?:var\s+ytInitialData\s*=|window\["ytInitialData"\]\s*=)\s*(\{.*?\})\s*;\s*</script>""",
        RegexOption.DOT_MATCHES_ALL,
    )

    private val VIDEO_ID = Regex("""^[A-Za-z0-9_-]{11}$""")

    data class Candidate(val videoId: String, val title: String)

    fun search(client: OkHttpClient, query: String): List<Candidate> {
        val url = "https://www.youtube.com/results?search_query=" +
            URLEncoder.encode(query, "UTF-8") + "&hl=en&gl=US"

        val html = try {
            val req = Request.Builder()
                .url(url)
                .header("User-Agent", USER_AGENT)
                .header("Accept-Language", "en-US,en;q=0.9")
                .build()
            client.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) {
                    err("results page HTTP ${resp.code} for \"$query\"")
                    return emptyList()
                }
                resp.body?.string()
            }
        } catch (e: Exception) {
            err("results page fetch failed for \"$query\": $e")
            return emptyList()
        } ?: return emptyList()

        val json = INITIAL_DATA.find(html)?.groupValues?.get(1)
        if (json == null) {
            val ids = Regex(""""videoId":"([A-Za-z0-9_-]{11})"""")
                .findAll(html).map { it.groupValues[1] }.distinct().take(10).toList()
            err("no ytInitialData for \"$query\"; raw id scan found ${ids.size}")
            return ids.map { Candidate(it, "") }
        }

        val root = try {
            JSONObject(json)
        } catch (e: Exception) {
            err("ytInitialData not valid JSON for \"$query\": $e")
            return emptyList()
        }

        val out = LinkedHashMap<String, Candidate>()
        collect(root, out, depth = 0)
        return out.values.take(15)
    }

    private fun collect(node: Any?, out: MutableMap<String, Candidate>, depth: Int) {
        if (depth > 40 || out.size >= 40) return
        when (node) {
            is JSONObject -> {
                val id = node.optString("videoId", "")
                if (VIDEO_ID.matches(id) && !out.containsKey(id)) {
                    val title = extractTitle(node)
                    if (title.isNotBlank()) out[id] = Candidate(id, title)
                }
                for (key in node.keys()) collect(node.opt(key), out, depth + 1)
            }
            is JSONArray -> {
                for (i in 0 until node.length()) collect(node.opt(i), out, depth + 1)
            }
        }
    }

    private fun extractTitle(node: JSONObject): String {
        val title = node.optJSONObject("title") ?: return ""
        title.optString("simpleText", "").let { if (it.isNotBlank()) return it }
        val runs = title.optJSONArray("runs") ?: return ""
        val sb = StringBuilder()
        for (i in 0 until runs.length()) {
            sb.append(runs.optJSONObject(i)?.optString("text", "") ?: "")
        }
        return sb.toString()
    }
}
