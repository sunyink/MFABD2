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

    data class Candidate(
        val release: GitHubReleasesApi.Release,
        val apk: GitHubReleasesApi.Asset,
        val metadata: GitHubReleasesApi.Asset,
        val code: Int,
    )

    fun latest(
        releases: List<GitHubReleasesApi.Release>,
        channel: UpdateChannel,
        abi: AndroidAbi,
    ): Candidate? {
        if (abi != AndroidAbi.ARM64) return null
        return releases.flatMap { release ->
            val version = UpdateVersion.parse(release.tag)
            if (version == null || !version.allowedFor(channel) ||
                (channel == UpdateChannel.STABLE && release.prerelease)) return@flatMap emptyList()
            val pattern = Regex("MFABD2-${Regex.escape(release.tag)}-android-arm64-vc([1-9][0-9]*)\\.apk")
            release.assets.mapNotNull { apk ->
                val code = pattern.matchEntire(apk.name)?.groupValues?.get(1)?.toIntOrNull()
                    ?.takeIf { it in 1..MAX_CODE } ?: return@mapNotNull null
                val metadata = release.assets.singleOrNull { it.name == "${apk.name}.json" }
                    ?: return@mapNotNull null
                if (release.assets.none { it.name == "${apk.name}.sha256" }) return@mapNotNull null
                Candidate(release, apk, metadata, code)
            }
        }.maxByOrNull { it.code }
    }

    /** Cross-check the selected sidecar instead of fetching one manifest per release. */
    fun digest(candidate: Candidate, metadata: JsonObject, applicationId: String): String? {
        fun integer(key: String) = (metadata[key] as? JsonPrimitive)
            ?.takeUnless { it.isString }?.intOrNull
        if (integer("schema_version") != 1 || integer("version_code") != candidate.code ||
            metadata.string("application_id") != applicationId ||
            metadata.string("certificate_sha256") != CERTIFICATE_SHA256 ||
            metadata.string("version_name") != candidate.release.tag ||
            metadata.string("apk_name") != candidate.apk.name) return null
        val digest = metadata.string("apk_sha256")?.takeIf(digestPattern::matches) ?: return null
        if (candidate.apk.sha256 != null && !candidate.apk.sha256.equals("sha256:$digest", ignoreCase = true)) return null
        return "sha256:$digest"
    }
}
