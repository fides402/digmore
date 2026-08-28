package com.digmore.newpipecli

import okhttp3.OkHttpClient
import org.schabi.newpipe.extractor.NewPipe
import org.schabi.newpipe.extractor.ServiceList
import org.schabi.newpipe.extractor.localization.ContentCountry
import org.schabi.newpipe.extractor.localization.Localization
import org.schabi.newpipe.extractor.stream.AudioStream
import org.schabi.newpipe.extractor.stream.DeliveryMethod
import org.schabi.newpipe.extractor.stream.StreamInfo
import org.schabi.newpipe.extractor.stream.StreamInfoItem
import org.schabi.newpipe.extractor.stream.StreamType
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

data class Candidate(
    val id: String,
    val watchUrl: String,
    val title: String,
    val uploader: String,
    val durationSec: Int?,
)

data class Resolved(
    val videoId: String,
    val title: String,
    val streamUrl: String,
    val durationSec: Int?,
)

/**
 * Search + resolve a playable direct audio URL for a track, entirely via
 * NewPipeExtractor (mimics the real InnerTube client, unlike yt-dlp's
 * web-scrape extractor) — no cookies, no API key. Ported from diggaplayer's
 * NewPipeYoutube.kt (app/src/main/java/com/music/spotui/youtube/), trimmed of
 * Android dependencies, to run standalone inside a GitHub Actions runner: the
 * same class of "datacenter IP" that digmore's own profiles.py documents as
 * blocked by YouTube for yt-dlp's plain HTML scraping path — this is the
 * on-device-proven workaround ("the jatz/diggaplayer system"), reused here.
 *
 * Scoring ported from digmore/engine/engine_libs/yt_hunter.py's _rank_entry:
 * token overlap with artist+title, penalize cover/live/remix, prefer
 * "official"/"audio", duration sanity 90-480s.
 */
object Resolver {
    private val initialized = AtomicBoolean(false)
    private val service = ServiceList.YouTube

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .build()

    private val BAD_WORDS = listOf(
        "live", "cover", "remix", "karaoke", "instrumental", "reaction",
        "tutorial", "lesson", "sped up", "slowed", "8d", "nightcore",
        "reverb", "loop", "type beat", "mashup", "edit", "remaster",
    )

    /** Must be called once, before [findBest]/[resolveStream]/[resolve], with
     *  the cookie header ([Cookies.loadHeader]) if the caller has one — after
     *  the first call the cookie header is locked in for the process, same as
     *  everything else NewPipe.init() sets up. */
    fun ensureInit(cookieHeader: String? = null) {
        if (initialized.compareAndSet(false, true)) {
            NewPipe.init(OkHttpNewPipeDownloader(httpClient, cookieHeader), Localization("en", "US"), ContentCountry("US"))
        }
    }

    private fun tokset(s: String): Set<String> =
        Regex("""\W+""").split(s.lowercase()).filter { it.length > 1 }.toSet()

    private fun score(title: String, artist: String, wantTitle: String, durationSec: Int?): Double {
        val name = title.lowercase()
        var s = 0.0
        val want = tokset(artist) + tokset(wantTitle)
        val have = tokset(name)
        if (want.isNotEmpty()) s += 3.0 * (want intersect have).size / want.size
        s -= BAD_WORDS.count { name.contains(it) } * 1.5
        if ("official" in name || "audio" in name) s += 0.5
        val dur = durationSec ?: 0
        if (dur in 90..480) s += 0.5
        return s
    }

    /** Search "artist - title" and "artist title", rank, pick the best candidate. */
    fun findBest(artist: String, title: String): Candidate? {
        ensureInit()
        val queries = listOf("$artist - $title", "$artist $title")
        var best: Candidate? = null
        var bestScore = Double.NEGATIVE_INFINITY
        for (query in queries) {
            val results = search(query)
            for (c in results) {
                val dur = c.durationSec ?: 0
                if (dur != 0 && (dur < 30 || dur > 1200)) continue
                val s = score(c.title, artist, title, c.durationSec)
                if (s > bestScore) {
                    bestScore = s
                    best = c
                }
            }
        }
        return best
    }

    private fun search(query: String): List<Candidate> {
        val fromNewPipe = runCatching { searchViaNewPipe(query) }
            .onFailure { err("NewPipe search failed for \"$query\": $it") }
            .getOrNull()
            ?.takeIf { it.isNotEmpty() }
        if (fromNewPipe != null) return fromNewPipe

        val fallback = SearchFallback.search(httpClient, query)
        return fallback.map { Candidate(it.videoId, "https://www.youtube.com/watch?v=${it.videoId}", it.title, "", null) }
    }

    private fun searchViaNewPipe(query: String): List<Candidate> {
        val extractor = service.getSearchExtractor(query, listOf("videos"), "")
        val page = extractor.initialPage
        val items = page.items
            .filterIsInstance<StreamInfoItem>()
            .filter { it.streamType == StreamType.VIDEO_STREAM }
            .take(15)
        return items.map {
            val watch = it.url.orEmpty()
            val videoId = Regex("""[?&]v=([A-Za-z0-9_-]{11})""").find(watch)?.groupValues?.get(1)
                ?: watch.substringAfterLast('/').substringBefore('?').takeIf { s -> s.length == 11 }
                ?: watch
            Candidate(
                id = videoId,
                watchUrl = watch.ifBlank { "https://www.youtube.com/watch?v=$videoId" },
                title = it.name.orEmpty(),
                uploader = it.uploaderName.orEmpty(),
                durationSec = it.duration.takeIf { d -> d > 0 }?.toInt(),
            )
        }
    }

    fun resolveStream(watchUrlOrVideoId: String): Pair<String, Int>? {
        ensureInit()
        val watchUrl = if (watchUrlOrVideoId.startsWith("http")) {
            watchUrlOrVideoId
        } else {
            "https://www.youtube.com/watch?v=$watchUrlOrVideoId"
        }
        val info = runCatching { StreamInfo.getInfo(service, watchUrl) }.getOrElse {
            err("StreamInfo.getInfo failed for $watchUrl: $it")
            return null
        }
        val audio = pickAudioStream(info.audioStreams) ?: run {
            err("no usable audio stream for $watchUrl (${info.audioStreams.size} streams)")
            return null
        }
        return audio.content to audio.averageBitrate.coerceAtLeast(0)
    }

    private fun pickAudioStream(streams: List<AudioStream>): AudioStream? =
        streams
            .filter { it.deliveryMethod == DeliveryMethod.PROGRESSIVE_HTTP || it.deliveryMethod == DeliveryMethod.DASH }
            .maxByOrNull { it.averageBitrate }

    /** Search + resolve in one call: what generate_cli.py actually needs. */
    fun resolve(artist: String, title: String): Resolved? {
        val cand = findBest(artist, title) ?: return null
        val (streamUrl, _) = resolveStream(cand.id) ?: return null
        return Resolved(cand.id, cand.title, streamUrl, cand.durationSec)
    }
}
