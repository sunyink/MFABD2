package com.aliothmoon.maafw.update

import io.mockk.coEvery
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import kotlin.coroutines.cancellation.CancellationException

class Mfabd2GitHubUpdateClientTest {
    private val tag = "v4.4.0"
    private val name = "MFABD2-$tag-android-arm64-vc701.apk"
    private val release = """[{"tag_name":"$tag","assets":[
        {"name":"$name","browser_download_url":"https://example.com/$name"},
        {"name":"$name.json","browser_download_url":"https://example.com/$name.json"},
        {"name":"$name.sha256","browser_download_url":"https://example.com/$name.sha256"}
    ]}]"""
    private val manifest = """{
        "schema_version":1,"application_id":"io.github.sunyink.mfabd2",
        "certificate_sha256":"${Mfabd2ReleasePolicy.CERTIFICATE_SHA256}",
        "version_name":"$tag","version_code":701,"apk_name":"$name",
        "apk_sha256":"${"a".repeat(64)}"
    }"""
    private fun client(gateway: RecordingHttpClientHelper, code: Int = 601) =
        Mfabd2GitHubUpdateClient(GitHubReleasesApi(gateway.mock), gateway.mock, code, "io.github.sunyink.mfabd2")
    private fun check() = UpdateCheckRequest(UpdateSource.GITHUB, "v99.0.0-beta.260909.fffffff",
        AndroidAbi.ARM64, githubRepository = "sunyink/MFABD2")
    private fun resolve() = UpdateResolveRequest(UpdateSource.GITHUB, AndroidAbi.ARM64,
        "v99.0.0-beta.260909.fffffff", githubRepository = "sunyink/MFABD2")
    private fun gateway(body: String = manifest) = RecordingHttpClientHelper(
        FakeHttpResponse(200, release), FakeHttpResponse(200, body))

    @Test
    fun installedCodeOverridesDisplayVersionAndOnlyOneManifestIsFetched() = runBlocking {
        val gateway = gateway()
        val result = client(gateway).check(check()) as UpdateCheckResult.UpdateAvailable
        assertEquals(tag, result.info.version)
        assertEquals(2, gateway.requests.size)
        assertEquals("https://example.com/$name.json", gateway.requests[1].first)
        assertTrue(gateway.requests.all { "Authorization" !in it.second })
    }

    @Test
    fun equalAndOlderCodesNeverOfferOrResolveAnUpdate() = runBlocking {
        for (code in listOf(701, 801)) {
            assertTrue(client(gateway(), code).check(check()) is UpdateCheckResult.UpToDate)
            assertEquals(UpdateCheckFailure.VERSION_INVALID,
                (client(gateway(), code).resolve(resolve()) as UpdateResolveResult.Failed).reason)
        }
    }

    @Test
    fun downloadUsesTheSameCandidateAndManifestHash() = runBlocking {
        val result = client(gateway()).resolve(resolve()) as UpdateResolveResult.Resolved
        assertEquals(ResolvedUpdate(UpdateSource.GITHUB, tag, "https://example.com/$name", "sha256:" + "a".repeat(64)),
            result.update)
    }

    @Test
    fun invalidManifestIsAnErrorNotASemverFallback() = runBlocking {
        for (body in listOf("not-json", manifest.replace("\"version_code\":701", "\"version_code\":901"))) {
            assertEquals(UpdateCheckFailure.INVALID_RESPONSE,
                (client(gateway(body)).check(check()) as UpdateCheckResult.SourceFailed).reason)
            assertEquals(UpdateCheckFailure.INVALID_RESPONSE,
                (client(gateway(body)).resolve(resolve()) as UpdateResolveResult.Failed).reason)
        }
    }

    @Test
    fun noAndroidAssetsAndHttpFailuresRemainExplicit() = runBlocking {
        val empty = RecordingHttpClientHelper(FakeHttpResponse(200, "[]"))
        assertEquals(UpdateCheckFailure.NO_MATCHING_ASSET,
            (client(empty).check(check()) as UpdateCheckResult.SourceFailed).reason)
        for (status in listOf(403, 429, 404)) {
            val failed = RecordingHttpClientHelper(FakeHttpResponse(200, release), FakeHttpResponse(status, ""))
            assertEquals(if (status == 404) UpdateCheckFailure.HTTP else UpdateCheckFailure.RATE_LIMITED,
                (client(failed).check(check()) as UpdateCheckResult.SourceFailed).reason)
        }
    }

    @Test
    fun cancellationIsNotConvertedToNetworkFailure() = runBlocking {
        val gateway = RecordingHttpClientHelper()
        coEvery { gateway.mock.get(any(), any(), any()) } throws CancellationException("cancel")
        try {
            client(gateway).check(check())
            fail("Cancellation must propagate")
        } catch (_: CancellationException) {
            // Expected: cancelling an update check must cancel its coroutine.
        }
    }
}
