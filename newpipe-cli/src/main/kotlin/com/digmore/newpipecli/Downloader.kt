package com.digmore.newpipecli

import okhttp3.OkHttpClient
import okhttp3.RequestBody.Companion.toRequestBody
import org.schabi.newpipe.extractor.downloader.Downloader
import org.schabi.newpipe.extractor.downloader.Request as NPRequest
import org.schabi.newpipe.extractor.downloader.Response as NPResponse
import org.schabi.newpipe.extractor.exceptions.ReCaptchaException
import java.io.IOException

/**
 * The [Downloader] NewPipeExtractor needs to make its own HTTP calls. Ported
 * verbatim (JVM-only, no Android dependency) from diggaplayer's
 * OkHttpNewPipeDownloader.kt (app/src/main/java/com/music/spotui/youtube/),
 * itself ported from jatz — same bridge, this time running inside the
 * GitHub Actions runner instead of on-device.
 */
private const val USER_AGENT =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

class OkHttpNewPipeDownloader(
    private val client: OkHttpClient,
    private val cookieHeader: String? = null,
) : Downloader() {

    @Throws(IOException::class, ReCaptchaException::class)
    override fun execute(request: NPRequest): NPResponse {
        val body = request.dataToSend()?.toRequestBody()

        val builder = okhttp3.Request.Builder()
            .url(request.url())
            .method(request.httpMethod(), body)

        var sawUserAgent = false
        for ((key, values) in request.headers()) {
            builder.removeHeader(key)
            for (v in values) builder.addHeader(key, v)
            if (key.equals("User-Agent", ignoreCase = true) && values.isNotEmpty()) {
                sawUserAgent = true
            }
        }
        if (!sawUserAgent) {
            builder.header("User-Agent", USER_AGENT)
        }
        if (!cookieHeader.isNullOrBlank()) {
            builder.header("Cookie", cookieHeader)
        }

        client.newCall(builder.build()).execute().use { resp ->
            if (resp.code == 429) {
                throw ReCaptchaException("reCaptcha challenge requested", request.url())
            }
            val responseBody = resp.body?.string()
            return NPResponse(
                resp.code,
                resp.message,
                resp.headers.toMultimap(),
                responseBody,
                resp.request.url.toString(),
            )
        }
    }
}
