package com.digmore.newpipecli

import kotlin.system.exitProcess

/**
 * Usage: newpipe-cli resolve --artist "..." --title "..."
 * Prints one line of JSON to stdout: {"videoId":"...","title":"...","streamUrl":"...","durationSec":123}
 * or {"error":"not_found"} with a non-zero exit code. All logging goes to stderr.
 */
fun main(args: Array<String>) {
    if (args.isEmpty() || args[0] != "resolve") {
        err("usage: newpipe-cli resolve --artist <artist> --title <title>")
        exitProcess(2)
    }
    var artist = ""
    var title = ""
    var i = 1
    while (i < args.size) {
        when (args[i]) {
            "--artist" -> { artist = args.getOrElse(i + 1) { "" }; i += 2 }
            "--title" -> { title = args.getOrElse(i + 1) { "" }; i += 2 }
            else -> i += 1
        }
    }
    if (artist.isBlank() && title.isBlank()) {
        err("both --artist and --title are empty")
        exitProcess(2)
    }

    val resolved = try {
        Resolver.resolve(artist, title)
    } catch (e: Exception) {
        err("resolve threw: $e")
        null
    }

    if (resolved == null) {
        println(jsonObj("error" to "not_found"))
        exitProcess(1)
    }

    println(
        jsonObj(
            "videoId" to resolved.videoId,
            "title" to resolved.title,
            "streamUrl" to resolved.streamUrl,
            "durationSec" to (resolved.durationSec ?: 0),
        )
    )
}

private fun jsonEscape(s: String): String {
    val sb = StringBuilder()
    for (c in s) {
        when (c) {
            '"' -> sb.append("\\\"")
            '\\' -> sb.append("\\\\")
            '\n' -> sb.append("\\n")
            '\r' -> sb.append("\\r")
            '\t' -> sb.append("\\t")
            else -> if (c.code < 0x20) sb.append("\\u%04x".format(c.code)) else sb.append(c)
        }
    }
    return sb.toString()
}

private fun jsonObj(vararg pairs: Pair<String, Any>): String =
    pairs.joinToString(",", prefix = "{", postfix = "}") { (k, v) ->
        val value = when (v) {
            is Int -> v.toString()
            is Long -> v.toString()
            else -> "\"${jsonEscape(v.toString())}\""
        }
        "\"${jsonEscape(k)}\":$value"
    }
