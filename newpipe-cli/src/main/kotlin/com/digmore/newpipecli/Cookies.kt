package com.digmore.newpipecli

import java.io.File

/**
 * Parses a Netscape-format cookies.txt (the format yt-dlp/browser export
 * extensions produce — same file digmore's own yt_hunter.py already reads
 * via YT_COOKIES_FILE) into a single `Cookie:` header value.
 *
 * Why this exists: StreamInfo.getInfo (the watch/player-response fetch) is
 * blocked on GitHub Actions runner IPs with SignInConfirmNotBotException —
 * confirmed live, search works fine, only that endpoint is gated. Cookies
 * from a real logged-in browser session authenticate the request as a real
 * user instead of an anonymous datacenter client, which is YouTube's own
 * documented workaround (it's exactly what YT_COOKIES/YT_COOKIES_FILE in
 * yt_hunter.py already exist for, on the yt-dlp side).
 */
object Cookies {
    fun loadHeader(path: String): String? {
        val file = File(path)
        if (!file.isFile) {
            err("cookie file not found: $path")
            return null
        }
        val pairs = mutableListOf<String>()
        file.forEachLine { rawLine ->
            val line = rawLine.trim()
            if (line.isEmpty() || line.startsWith("#")) return@forEachLine
            val cols = line.split("\t")
            if (cols.size < 7) return@forEachLine
            val name = cols[5]
            val value = cols[6]
            if (name.isNotBlank()) pairs.add("$name=$value")
        }
        if (pairs.isEmpty()) {
            err("cookie file had no usable entries: $path")
            return null
        }
        err("loaded ${pairs.size} cookies from $path")
        return pairs.joinToString("; ")
    }
}
