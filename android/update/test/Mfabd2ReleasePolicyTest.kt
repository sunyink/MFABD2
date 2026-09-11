package com.aliothmoon.maafw.update

import com.aliothmoon.maafw.util.parseJsonObject
import org.junit.Assert.*
import org.junit.Test

class Mfabd2ReleasePolicyTest {
    private fun assetNames(tag: String) = "MFABD2-$tag-android-arm64.apk"

    private fun release(tag: String, complete: Boolean = true): GitHubReleasesApi.Release {
        val name = assetNames(tag)
        val names = if (complete) listOf(name, "$name.json", "$name.sha256") else listOf(name)
        return GitHubReleasesApi.Release(tag, null, null,
            names.map { GitHubReleasesApi.Asset(it, "https://example.com/$it", null) }, '-' in tag)
    }

    private fun candidates(vararg releases: GitHubReleasesApi.Release,
                           channel: UpdateChannel = UpdateChannel.BETA) =
        Mfabd2ReleasePolicy.candidates(releases.toList(), channel, AndroidAbi.ARM64)

    private fun manifest(
        candidate: Mfabd2ReleasePolicy.Candidate,
        tag: String = "v4.4.0",
        code: Int = 601,
    ) = """{
        "schema_version":1,"application_id":"io.github.sunyink.mfabd2",
        "certificate_sha256":"${Mfabd2ReleasePolicy.CERTIFICATE_SHA256}",
        "version_name":"$tag","version_code":$code,
        "apk_name":"${candidate.apk.name}","apk_sha256":"${"a".repeat(64)}"
    }"""

    private fun sequence(candidate: Mfabd2ReleasePolicy.Candidate, body: String) =
        Mfabd2ReleasePolicy.sequence(candidate, parseJsonObject(body)!!, "io.github.sunyink.mfabd2")

    @Test
    fun assetNameMatchesTheOtherPlatformsWithNoSequenceNumber() {
        // The install sequence lives in the sidecar; the APK is named like every
        // other platform's asset so a release page reads consistently.
        val candidate = candidates(release("v4.4.0")).single()
        assertEquals("MFABD2-v4.4.0-android-arm64.apk", candidate.apk.name)
        assertEquals("MFABD2-v4.4.0-android-arm64.apk.json", candidate.metadata.name)
    }

    @Test
    fun channelSelectionAppliesBeforeAnythingElse() {
        val stable = release("v4.4.0")
        val beta = release("v4.4.1-beta.260909.aaaaaa")
        val alpha = release("v4.4.2-alpha.260909.aaaaaa")
        val ci = release("v4.4.0-ci.260909.aaaaaa")
        assertEquals(listOf(stable.tag),
            candidates(stable, beta, alpha, ci, channel = UpdateChannel.STABLE).map { it.release.tag })
        assertTrue(candidates(beta, alpha, ci, channel = UpdateChannel.STABLE).isEmpty())
        assertTrue(candidates(stable.copy(prerelease = true), channel = UpdateChannel.STABLE).isEmpty())
    }

    @Test
    fun incompleteUploadsLegacyNamesAndCiBuildsAreNotAdvertised() {
        val ready = release("v4.4.0")
        val incomplete = release("v4.4.1", complete = false)
        // The pre-rename asset carried the sequence number in its filename.
        val legacy = release("v4.4.2").let { it.copy(assets = it.assets.map { asset ->
            asset.copy(name = asset.name.replace("-arm64.apk", "-arm64-vc801.apk"))
        }) }
        // The CI identity is signed by a different key and must never be offered
        // as an update to the released app.
        val ciIdentity = release("v4.4.3").let { it.copy(assets = it.assets.map { asset ->
            asset.copy(name = asset.name.replace("-arm64.apk", "-arm64-ci.apk"))
        }) }
        assertEquals(listOf(ready.tag),
            candidates(incomplete, legacy, ciIdentity, ready).map { it.release.tag })
    }

    @Test
    fun onlyArm64IsEligible() {
        assertTrue(Mfabd2ReleasePolicy.candidates(
            listOf(release("v4.4.0")), UpdateChannel.STABLE, AndroidAbi.X86_64).isEmpty())
    }

    @Test
    fun duplicateApksForOneTagAreRejectedRatherThanGuessed() {
        val single = release("v4.4.0")
        val doubled = single.copy(assets = single.assets + single.assets.first())
        assertTrue(candidates(doubled).isEmpty())
    }

    @Test
    fun onlyAFewRecentReleasesAreScanned() {
        val many = (0..9).map { release("v4.4.$it") }
        val scanned = Mfabd2ReleasePolicy.candidates(many, UpdateChannel.STABLE, AndroidAbi.ARM64)
        assertEquals(3, scanned.size)
        assertEquals(many.take(3).map { it.tag }, scanned.map { it.release.tag })
    }

    @Test
    fun sequenceComesFromTheSidecarAndIsRangeChecked() {
        val candidate = candidates(release("v4.4.0")).single()
        assertEquals(601, sequence(candidate, manifest(candidate)))
        // Release order need not match install order: a rebuild of the same tag
        // simply reports a larger number, with no filename change involved.
        assertEquals(70201, sequence(candidate, manifest(candidate, code = 70201)))
        for (code in listOf(0, -1, 2_100_000_001))
            assertNull("code $code", sequence(candidate, manifest(candidate, code = code)))
    }

    @Test
    fun manifestMustDescribeExactlyThisApkOfThisApp() {
        val candidate = candidates(release("v4.4.0")).single()
        val body = manifest(candidate)
        assertEquals(601, sequence(candidate, body))
        for ((from, to) in listOf(
            "\"schema_version\":1" to "\"schema_version\":2",
            "\"version_code\":601" to "\"version_code\":\"601\"",
            "io.github.sunyink.mfabd2" to "com.aliothmoon.maafw",
            Mfabd2ReleasePolicy.CERTIFICATE_SHA256 to "b".repeat(64),
            "\"version_name\":\"v4.4.0\"" to "\"version_name\":\"v4.4.1\"",
            "-arm64.apk" to "-arm64-ci.apk",
        )) assertNull("mismatch: $from", sequence(candidate, body.replace(from, to)))
    }

    @Test
    fun digestIsCrossCheckedAgainstTheAssetItself() {
        val candidate = candidates(release("v4.4.0")).single()
        val body = manifest(candidate)
        fun digest(c: Mfabd2ReleasePolicy.Candidate, value: String) =
            Mfabd2ReleasePolicy.digest(c, parseJsonObject(value)!!)
        assertEquals("sha256:" + "a".repeat(64), digest(candidate, body))
        assertNull(digest(candidate, body.replace("a".repeat(64), "bad-hash")))
        // GitHub reporting a different digest than the sidecar stops the download.
        assertNull(digest(candidate.copy(apk = candidate.apk.copy(sha256 = "sha256:" + "b".repeat(64))), body))
        assertEquals("sha256:" + "a".repeat(64),
            digest(candidate.copy(apk = candidate.apk.copy(sha256 = "SHA256:" + "A".repeat(64))), body))
    }
}
