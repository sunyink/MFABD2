package com.aliothmoon.maafw.update

import com.aliothmoon.maafw.util.string
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.intOrNull

/** Installation order is independent of the user-facing project version. */
internal object Mfabd2ReleasePolicy {
    const val CERTIFICATE_SHA256 = "@CERTIFICATE_SHA256@"
    private const val MAX_CODE = 2_100_000_000
    private val digestPattern = Regex("[0-9a-f]{64}")

    /** How many recent releases get their sidecar read before picking a winner. */
    private const val SCAN_DEPTH = 3

    data class Candidate(
        val release: GitHubReleasesApi.Release,
        val apk: GitHubReleasesApi.Asset,
        val metadata: GitHubReleasesApi.Asset,
    )

    /** Release order does not have to match install order, so scan a few and compare
     *  their sequence numbers. The number lives in the sidecar, not the filename:
     *  the APK is named like every other platform's asset. */
    fun candidates(
        releases: List<GitHubReleasesApi.Release>,
        channel: UpdateChannel,
        abi: AndroidAbi,
    ): List<Candidate> {
        if (abi != AndroidAbi.ARM64) return emptyList()
        return releases.mapNotNull { release ->
            val version = UpdateVersion.parse(release.tag)
            if (version == null || !version.allowedFor(channel) ||
                (channel == UpdateChannel.STABLE && release.prerelease)) return@mapNotNull null
            // Exact match, so the CI identity's `-ci.apk` can never be offered here.
            val name = "MFABD2-${release.tag}-android-arm64.apk"
            val apk = release.assets.singleOrNull { it.name == name } ?: return@mapNotNull null
            val metadata = release.assets.singleOrNull { it.name == "$name.json" }
                ?: return@mapNotNull null
            if (release.assets.none { it.name == "$name.sha256" }) return@mapNotNull null
            Candidate(release, apk, metadata)
        }.take(SCAN_DEPTH)
    }

    /** Returns the install sequence number, or null when the sidecar does not describe
     *  exactly this APK of this app signed by this key. */
    fun sequence(candidate: Candidate, metadata: JsonObject, applicationId: String): Int? {
        fun integer(key: String) = (metadata[key] as? JsonPrimitive)
            ?.takeUnless { it.isString }?.intOrNull
        if (integer("schema_version") != 1 ||
            metadata.string("application_id") != applicationId ||
            metadata.string("certificate_sha256") != CERTIFICATE_SHA256 ||
            metadata.string("version_name") != candidate.release.tag ||
            metadata.string("apk_name") != candidate.apk.name) return null
        return integer("version_code")?.takeIf { it in 1..MAX_CODE }
    }

    fun digest(candidate: Candidate, metadata: JsonObject): String? {
        val digest = metadata.string("apk_sha256")?.takeIf(digestPattern::matches) ?: return null
        if (candidate.apk.sha256 != null && !candidate.apk.sha256.equals("sha256:$digest", ignoreCase = true)) return null
        return "sha256:$digest"
    }
}
