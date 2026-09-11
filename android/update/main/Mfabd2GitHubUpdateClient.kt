package com.aliothmoon.maafw.update

import com.aliothmoon.maafw.BuildConfig
import com.aliothmoon.maafw.i18n.uiTextFromFramework
import com.aliothmoon.maafw.util.HttpClientHelper
import com.aliothmoon.maafw.util.parseJsonObject
import com.aliothmoon.maafw.util.readBody
import timber.log.Timber
import kotlin.coroutines.cancellation.CancellationException

/** MFABD2 uses one Android build sequence across all project release channels. */
internal class Mfabd2GitHubUpdateClient(
    private val api: GitHubReleasesApi,
    private val helper: HttpClientHelper,
    private val currentCode: Int = BuildConfig.VERSION_CODE,
    private val applicationId: String = BuildConfig.APPLICATION_ID,
) : UpdateSourceClient {
    override val source = UpdateSource.GITHUB

    private data class Verified(
        val candidate: Mfabd2ReleasePolicy.Candidate,
        val digest: String,
        val code: Int,
    )

    private suspend fun latest(repository: String?, channel: UpdateChannel, abi: AndroidAbi): UpdateSourceOutcome<Verified> {
        val repo = api.parseRepository(repository)
            ?: return UpdateSourceOutcome.Failed(UpdateCheckFailure.MISSING_CONFIGURATION)
        val releases = when (val result = api.releases(repo)) {
            is UpdateSourceOutcome.Failed -> return result
            is UpdateSourceOutcome.Ok -> result.value
        }
        val candidates = Mfabd2ReleasePolicy.candidates(releases, channel, abi)
        if (candidates.isEmpty()) return UpdateSourceOutcome.Failed(UpdateCheckFailure.NO_MATCHING_ASSET)
        var best: Verified? = null
        var failure: UpdateCheckFailure? = null
        for (candidate in candidates) {
            val response = helper.get(candidate.metadata.downloadUrl)
            val status = response.code
            val body = response.readBody()
            if (status !in 200..299) {
                // Rate limiting says nothing about this release; remember it only if
                // no candidate ends up usable, and keep looking.
                failure = if (status == 403 || status == 429) UpdateCheckFailure.RATE_LIMITED
                else UpdateCheckFailure.HTTP
                continue
            }
            val metadata = parseJsonObject(body)
            val code = metadata?.let { Mfabd2ReleasePolicy.sequence(candidate, it, applicationId) }
            val digest = metadata?.let { Mfabd2ReleasePolicy.digest(candidate, it) }
            if (code == null || digest == null) {
                // A damaged or mismatched sidecar is never silently treated as "older".
                failure = failure ?: UpdateCheckFailure.INVALID_RESPONSE
                continue
            }
            if (best == null || code > best.code) best = Verified(candidate, digest, code)
        }
        return best?.let { UpdateSourceOutcome.Ok(it) }
            ?: UpdateSourceOutcome.Failed(failure ?: UpdateCheckFailure.NO_MATCHING_ASSET)
    }

    override suspend fun check(request: UpdateCheckRequest): UpdateCheckResult = try {
        when (val result = latest(request.githubRepository, request.channel, request.abi)) {
            is UpdateSourceOutcome.Failed -> UpdateCheckResult.SourceFailed(source, result.reason, result.detail)
            is UpdateSourceOutcome.Ok -> {
                val release = result.value.candidate.release
                if (result.value.code <= currentCode) UpdateCheckResult.UpToDate(source, release.tag)
                else UpdateCheckResult.UpdateAvailable(
                    source, UpdateInfo(release.tag, release.htmlUrl, release.body),
                )
            }
        }
    } catch (e: CancellationException) {
        throw e
    } catch (e: Exception) {
        Timber.tag("UpdateCheck").w(e, "MFABD2 GitHub check failed")
        UpdateCheckResult.SourceFailed(source, UpdateCheckFailure.NETWORK)
    }

    override suspend fun resolve(request: UpdateResolveRequest): UpdateResolveResult = try {
        when (val result = latest(request.githubRepository, request.channel, request.abi)) {
            is UpdateSourceOutcome.Failed -> UpdateResolveResult.Failed(source, result.reason, result.detail)
            is UpdateSourceOutcome.Ok -> {
                val candidate = result.value.candidate
                if (result.value.code <= currentCode) UpdateResolveResult.Failed(
                    source, UpdateCheckFailure.VERSION_INVALID,
                    uiTextFromFramework("所选构建不比当前安装版本新。安装更早的构建需要卸载重装。"),
                ) else UpdateResolveResult.Resolved(
                    ResolvedUpdate(source, candidate.release.tag, candidate.apk.downloadUrl, result.value.digest),
                )
            }
        }
    } catch (e: CancellationException) {
        throw e
    } catch (e: Exception) {
        Timber.tag("UpdateResolve").w(e, "MFABD2 GitHub resolve failed")
        UpdateResolveResult.Failed(source, UpdateCheckFailure.NETWORK)
    }
}
