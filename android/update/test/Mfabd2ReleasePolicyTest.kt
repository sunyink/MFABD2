package com.aliothmoon.maafw.update

import com.aliothmoon.maafw.util.parseJsonObject
import org.junit.Assert.*
import org.junit.Test

class Mfabd2ReleasePolicyTest {
    private fun release(tag: String, code: Int, complete: Boolean = true): GitHubReleasesApi.Release {
        val name = "MFABD2-$tag-android-arm64-vc$code.apk"
        val names = if (complete) listOf(name, "$name.json", "$name.sha256") else listOf(name)
        return GitHubReleasesApi.Release(tag, null, null,
            names.map { GitHubReleasesApi.Asset(it, "https://example.com/$it", null) }, '-' in tag)
    }

    private fun latest(vararg releases: GitHubReleasesApi.Release, channel: UpdateChannel = UpdateChannel.BETA) =
        Mfabd2ReleasePolicy.latest(releases.toList(), channel, AndroidAbi.ARM64)

    @Test
    fun sameDayShaDoesNotOrderUpdates() {
        val first = release("v4.4.1-beta.260909.fffffff", 601)
        val second = release("v4.4.1-beta.260909.aaaaaaa", 701)
        assertEquals(701, latest(second, first)?.code)
        assertEquals(second.tag, latest(first, second)?.release?.tag)
    }

    @Test
    fun laterStableCanReplaceAnEarlierTestWithHigherDisplayVersion() {
        val beta = release("v4.4.1-beta.260909.ffffff", 601)
        val stable = release("v4.4.0", 701)
        assertEquals(stable, latest(beta, stable)?.release)
        assertEquals(stable, latest(beta, stable, channel = UpdateChannel.STABLE)?.release)
    }

    @Test
    fun channelSelectionStillAppliesBeforeNumericOrdering() {
        val stable = release("v4.4.0", 601)
        val beta = release("v4.4.1-beta.260909.aaaaaa", 701)
        val alpha = release("v4.4.2-alpha.260909.aaaaaa", 801)
        val ci = release("v4.4.0-ci.260909.aaaaaa", 901)
        assertEquals(601, latest(stable, beta, alpha, ci, channel = UpdateChannel.STABLE)?.code)
        assertEquals(701, latest(stable, beta, alpha, ci)?.code)
        assertNull(latest(stable.copy(prerelease = true), channel = UpdateChannel.STABLE))
    }

    @Test
    fun incompleteUploadsAndLegacyApksAreNotAdvertised() {
        val ready = release("v4.4.0", 601)
        val incomplete = release("v4.4.1", 701, complete = false)
        val legacy = release("v4.4.2", 801).let { it.copy(assets = it.assets.map { asset ->
            asset.copy(name = asset.name.replace("-vc801", ""))
        }) }
        assertEquals(601, latest(incomplete, legacy, ready)?.code)
        assertNull(latest(incomplete, legacy))
    }

    @Test
    fun onlyArm64AndValidPositiveCodesAreEligible() {
        val ready = release("v4.4.0", 601)
        assertNull(Mfabd2ReleasePolicy.latest(listOf(ready), UpdateChannel.STABLE, AndroidAbi.X86_64))
        assertNull(latest(release("v4.4.0", 0), release("v4.4.0", -1), release("v4.4.0", Int.MAX_VALUE)))
        assertEquals(2_100_000_000, latest(release("v4.4.0", 2_100_000_000))?.code)
    }

    @Test
    fun multipleBuildsOfOneTagSelectTheMatchingNewestApk() {
        val first = release("v4.4.0", 601)
        val second = release("v4.4.0", 602)
        assertEquals(602, latest(first.copy(assets = first.assets + second.assets))?.code)
        assertEquals(701, latest(first, release("v4.3.0", 701))?.code)
    }

    @Test
    fun manifestMustMatchCodeIdentityTagFilenameAndDigest() {
        val candidate = latest(release("v4.4.0", 601))!!
        val body = """{
            "schema_version":1,"application_id":"io.github.sunyink.mfabd2",
            "certificate_sha256":"${Mfabd2ReleasePolicy.CERTIFICATE_SHA256}",
            "version_name":"v4.4.0","version_code":601,
            "apk_name":"${candidate.apk.name}","apk_sha256":"${"a".repeat(64)}"
        }"""
        fun digest(value: String) = Mfabd2ReleasePolicy.digest(candidate, parseJsonObject(value)!!, "io.github.sunyink.mfabd2")
        assertEquals("sha256:" + "a".repeat(64), digest(body))
        for ((from, to) in listOf(
            "\"schema_version\":1" to "\"schema_version\":2",
            "\"version_code\":601" to "\"version_code\":602",
            "\"version_code\":601" to "\"version_code\":\"601\"",
            "io.github.sunyink.mfabd2" to "com.aliothmoon.maafw",
            Mfabd2ReleasePolicy.CERTIFICATE_SHA256 to "b".repeat(64),
            "\"version_name\":\"v4.4.0\"" to "\"version_name\":\"v4.4.1\"",
            "-vc601.apk" to "-vc602.apk",
            "a".repeat(64) to "bad-hash",
        )) assertNull("mismatch: $from", digest(body.replace(from, to)))
        assertNull(Mfabd2ReleasePolicy.digest(candidate.copy(apk = candidate.apk.copy(sha256 = "sha256:" + "b".repeat(64))),
            parseJsonObject(body)!!, "io.github.sunyink.mfabd2"))
    }
}
