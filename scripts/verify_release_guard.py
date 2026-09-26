"""Offline lifecycle tests: no GitHub writes, tokens, or real release artifacts."""
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from release_guard import (
    Build, DESKTOP_TARGETS, GitHub, GitHubError, ReleaseError, cleanup, cleanup_target,
    expected_assets, local_assets, prepare, publish, read_receipt, release_body,
    resolve_tag, upload, verify_publication, verify_uploaded_assets,
)

ROOT = Path(__file__).resolve().parents[1]
TAG = "v4.4.5-beta.260925.49496bfd"
SHA = "49496bfd99d8eef6f5696801f7c2c5f8361f38d4"
BUILD = Build("example/test", TAG, SHA, "release", 519, 1, True)


class FakeGitHub:
    repository = BUILD.repository

    def __init__(self):
        self.releases = {}
        self.assets = {}
        self.refs = {}
        self.tags = {}
        self.runs = {}
        self.mutations = []
        self.next_id = 100
        self.next_asset = 1000
        self.lost_publish_reply = False
        self.fail_upload = None
        self.read_hook = None

    def optional(self, endpoint):
        try:
            return self.request("GET", endpoint)
        except GitHubError as error:
            if error.status == 404:
                return None
            raise

    def listing(self, endpoint):
        if endpoint == "releases":
            return copy.deepcopy(list(self.releases.values()))
        return copy.deepcopy(self.assets[int(endpoint.split('/')[1])])

    def request(self, method, endpoint, body=None):
        if method != "GET":
            self.mutations.append((method, endpoint, copy.deepcopy(body)))
        if endpoint.startswith("git/ref/tags/"):
            tag = endpoint.removeprefix("git/ref/tags/")
            if tag not in self.refs:
                raise GitHubError(method, endpoint, 404)
            return {"object": copy.deepcopy(self.refs[tag])}
        if endpoint.startswith("git/tags/"):
            return {"object": copy.deepcopy(self.tags[endpoint.split('/')[-1]])}
        if endpoint == "git/refs":
            self.refs[body['ref'].removeprefix('refs/tags/')] = {"type": "commit", "sha": body['sha']}
            return {}
        if endpoint.startswith("actions/runs/"):
            parts = endpoint.split('/')
            return copy.deepcopy(self.runs[(int(parts[2]), int(parts[4]))])
        if endpoint == "releases" and method == "POST":
            identifier = self.next_id
            self.next_id += 1
            value = {**body, "id": identifier,
                     "upload_url": f"https://uploads.github.com/repos/example/test/releases/{identifier}/assets{{?name,label}}"}
            self.releases[identifier] = value
            self.assets[identifier] = []
            return copy.deepcopy(value)
        if endpoint.startswith("releases/assets/") and method == "DELETE":
            identifier = int(endpoint.split('/')[-1])
            for rid in self.assets:
                self.assets[rid] = [a for a in self.assets[rid] if a['id'] != identifier]
            return None
        if endpoint.startswith("releases/"):
            identifier = int(endpoint.split('/')[1])
            if self.read_hook and method == "GET":
                self.read_hook(identifier)
            if identifier not in self.releases:
                raise GitHubError(method, endpoint, 404)
            value = self.releases[identifier]
            if method == "PATCH":
                value.update(body)
                if body.get('draft') is False and self.lost_publish_reply:
                    self.lost_publish_reply = False
                    raise URLError('response lost after publication')
            if method == "DELETE":
                del self.releases[identifier]
                del self.assets[identifier]
                return None
            return copy.deepcopy(value)
        raise AssertionError((method, endpoint))

    def upload(self, release, path):
        self.mutations.append(("UPLOAD", release['id'], path.name))
        if path.name == self.fail_upload:
            raise URLError('upload interrupted')
        value = {"id": self.next_asset, "name": path.name, "state": "uploaded",
                 "size": path.stat().st_size, "digest": 'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()}
        self.next_asset += 1
        self.assets[release['id']].append(value)
        return value

    def finished(self, owner=BUILD, *, status='completed', conclusion='cancelled', **kwargs):
        self.runs[(owner.run_id, owner.attempt)] = {
            "status": status, "conclusion": conclusion, "head_sha": owner.source_sha,
            "run_attempt": owner.attempt, "path": ".github/workflows/install.yml", **kwargs,
        }


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        for name in expected_assets(TAG, 'release'):
            (self.directory/name).write_bytes(name.encode())
        self.expected = local_assets(self.directory, TAG, 'release')
        self.api = FakeGitHub()

    def prepared(self, build=BUILD):
        identifier, public = prepare(self.api, self.directory, build, 'Human notes')
        self.assertFalse(public)
        return identifier

    def complete(self, build=BUILD):
        identifier = self.prepared(build)
        upload(self.api, self.directory, build, identifier)
        return identifier

    def assert_not_published(self, identifier):
        self.assertTrue(self.api.releases[identifier]['draft'])
        self.assertFalse(any(m == 'PATCH' and isinstance(b,dict) and b.get('draft') is False
                             for m,e,b in self.api.mutations))

    def test_complete_pre_and_stable_publication(self):
        for prerelease in (True,False):
            self.api = FakeGitHub()
            build = replace(BUILD,prerelease=prerelease)
            identifier = self.complete(build)
            publish(self.api,self.directory,build,identifier)
            self.assertFalse(self.api.releases[identifier]['draft'])
            self.assertEqual(self.api.releases[identifier]['prerelease'],prerelease)

    def test_missing_or_empty_local_file_prevents_any_remote_mutation(self):
        path = self.directory/'CHANGES.zip'
        path.unlink()
        with self.assertRaises(ReleaseError):self.prepared()
        path.write_bytes(b'')
        with self.assertRaises(ReleaseError):self.prepared()
        self.assertEqual(self.api.mutations,[])

    def test_unexpected_local_asset(self):
        (self.directory/'old.zip').write_text('bad')
        with self.assertRaises(ReleaseError):self.prepared()
        self.assertEqual(self.api.mutations,[])

    def test_wrong_actual_tag_is_rejected_even_with_matching_metadata(self):
        self.api.refs[TAG]={'type':'commit','sha':'b'*40}
        with self.assertRaisesRegex(ReleaseError,'Actual tag'):self.prepared()
        self.assertEqual(self.api.mutations,[])

    def test_annotated_tag_is_peeled(self):
        self.api.refs[TAG]={'type':'tag','sha':'c'*40}
        self.api.tags['c'*40]={'type':'tag','sha':'d'*40}
        self.api.tags['d'*40]={'type':'commit','sha':SHA}
        self.assertEqual(resolve_tag(self.api,TAG),SHA)
        self.prepared()
        self.assertFalse(any(e=='git/refs' for m,e,b in self.api.mutations))

    def test_tag_changed_before_publication(self):
        identifier=self.complete()
        self.api.refs[TAG]['sha']='b'*40
        with self.assertRaisesRegex(ReleaseError,'Actual tag'):publish(self.api,self.directory,BUILD,identifier)
        self.assert_not_published(identifier)

    def test_partial_or_failed_upload_remains_private(self):
        identifier=self.prepared()
        self.api.fail_upload=sorted(self.expected)[1]
        with self.assertRaises(URLError):upload(self.api,self.directory,BUILD,identifier)
        with self.assertRaises(ReleaseError):publish(self.api,self.directory,BUILD,identifier)
        self.assert_not_published(identifier)

    def test_missing_wrong_digest_size_and_upload_state_block_publication(self):
        for key,value in [('digest',None),('digest','sha256:'+'0'*64),('size',0),('state','starter')]:
            self.api=FakeGitHub();identifier=self.complete()
            self.api.assets[identifier][0][key]=value
            with self.assertRaises(ReleaseError):publish(self.api,self.directory,BUILD,identifier)
            self.assert_not_published(identifier)
        self.api=FakeGitHub();identifier=self.complete();self.api.assets[identifier].pop()
        with self.assertRaises(ReleaseError):publish(self.api,self.directory,BUILD,identifier)
        self.assert_not_published(identifier)

    def test_duplicate_remote_asset_is_rejected(self):
        identifier=self.complete()
        self.api.assets[identifier].append(copy.deepcopy(self.api.assets[identifier][0]))
        with self.assertRaises(ReleaseError):publish(self.api,self.directory,BUILD,identifier)
        self.assert_not_published(identifier)

    def test_duplicate_release_appearing_after_prepare_is_rejected(self):
        identifier=self.complete()
        self.api.request('POST','releases',{**self.api.releases[identifier]})
        with self.assertRaisesRegex(ReleaseError,'not unique'):publish(self.api,self.directory,BUILD,identifier)
        self.assert_not_published(identifier)

    def test_same_attempt_rerun_reuses_id_and_matching_assets(self):
        identifier=self.complete()
        again=self.prepared()
        before=len(self.api.mutations)
        upload(self.api,self.directory,BUILD,again)
        self.assertEqual(identifier,again)
        self.assertEqual(len(self.api.mutations),before)

    def test_changed_draft_asset_is_replaced_only_in_draft(self):
        identifier=self.complete()
        (self.directory/'CHANGES.zip').write_text('changed draft')
        self.assertEqual(self.prepared(),identifier)
        upload(self.api,self.directory,BUILD,identifier)
        self.assertTrue(any(m=='DELETE' and e.startswith('releases/assets/') for m,e,b in self.api.mutations))
        publish(self.api,self.directory,BUILD,identifier)

    def test_unowned_draft_is_not_claimed_or_deleted(self):
        identifier=self.api.request('POST','releases',{'tag_name':TAG,'target_commitish':SHA,'draft':True,'body':'manual draft'})['id']
        before=len(self.api.mutations)
        with self.assertRaisesRegex(ReleaseError,'receipt'):self.prepared()
        cleanup(self.api,BUILD.run_id,BUILD.attempt,SHA)
        self.assertEqual(len(self.api.mutations),before)
        self.assertIn(identifier,self.api.releases)
        self.assertNotIn(TAG,self.api.refs)

    def test_active_attempt_cannot_be_claimed(self):
        identifier=self.prepared()
        self.api.finished(status='in_progress',conclusion=None)
        with self.assertRaisesRegex(ReleaseError,'still owns'):
            prepare(self.api,self.directory,replace(BUILD,attempt=2))
        self.assertEqual(read_receipt(self.api.releases[identifier])[0],BUILD)

    def test_completed_attempt_can_be_adopted_and_old_cleanup_skips_it(self):
        identifier=self.prepared()
        self.api.finished()
        second=replace(BUILD,attempt=2)
        self.assertEqual(prepare(self.api,self.directory,second)[0],identifier)
        cleanup(self.api,BUILD.run_id,BUILD.attempt,SHA,completed=True,tag=TAG)
        self.assertIn(identifier,self.api.releases)
        self.assertEqual(read_receipt(self.api.releases[identifier])[0],second)

    def test_older_attempt_cannot_reclaim_a_newer_draft(self):
        second=replace(BUILD,attempt=2)
        self.prepared(second);self.api.finished(second)
        with self.assertRaisesRegex(ReleaseError,'older attempt'):self.prepared()

    def test_cleanup_ownership_rechecked_after_waiting_for_lock(self):
        identifier=self.prepared();self.api.finished()
        self.assertEqual(cleanup_target(self.api,BUILD.run_id,BUILD.attempt,SHA),TAG)
        newer=replace(BUILD,attempt=2)
        def adopt(rid):self.api.releases[rid]['body']=release_body('',newer,self.expected)
        self.api.read_hook=adopt
        cleanup(self.api,BUILD.run_id,BUILD.attempt,SHA,completed=True,tag=TAG)
        self.assertIn(identifier,self.api.releases)

    def test_completed_cleanup_deletes_only_owned_draft_and_keeps_tag(self):
        identifier=self.prepared();self.api.finished()
        cleanup(self.api,BUILD.run_id,BUILD.attempt,SHA,completed=True,tag=TAG)
        self.assertNotIn(identifier,self.api.releases)
        self.assertEqual(resolve_tag(self.api,TAG),SHA)

    def test_lost_draft_creation_response_can_be_cleaned_without_output_id(self):
        original=self.api.request
        def request(method,endpoint,body=None):
            result=original(method,endpoint,body)
            if method=='POST' and endpoint=='releases':
                raise URLError('draft created but response/output lost')
            return result
        self.api.request=request
        with self.assertRaises(URLError):self.prepared()
        self.assertEqual(len(self.api.releases),1)
        cleanup(self.api,BUILD.run_id,BUILD.attempt,SHA)
        self.assertEqual(self.api.releases,{})
        self.assertEqual(resolve_tag(self.api,TAG),SHA)

    def test_cleanup_rechecks_draft_state_immediately_before_deletion(self):
        identifier=self.prepared();self.api.finished()
        def published(rid):self.api.releases[rid]['draft']=False
        self.api.read_hook=published
        cleanup(self.api,BUILD.run_id,BUILD.attempt,SHA,completed=True)
        self.assertIn(identifier,self.api.releases)

    def test_cleanup_does_not_touch_public_releases(self):
        identifier=self.complete();publish(self.api,self.directory,BUILD,identifier)
        self.api.finished()
        cleanup(self.api,BUILD.run_id,BUILD.attempt,SHA,completed=True)
        self.assertIn(identifier,self.api.releases)
        self.assertEqual(cleanup_target(self.api,BUILD.run_id,BUILD.attempt,SHA),'')

    def test_completed_cleanup_does_not_delete_running_or_successful_attempt(self):
        for status,conclusion in [('in_progress',None),('completed','success')]:
            self.api=FakeGitHub();identifier=self.prepared();self.api.finished(status=status,conclusion=conclusion)
            with self.assertRaises(ReleaseError):cleanup_target(self.api,BUILD.run_id,BUILD.attempt,SHA)
            cleanup(self.api,BUILD.run_id,BUILD.attempt,SHA,completed=True)
            self.assertIn(identifier,self.api.releases)

    def test_cleanup_rejects_wrong_workflow_or_source(self):
        identifier=self.prepared();self.api.finished(path='.github/workflows/other.yml')
        with self.assertRaises(ReleaseError):cleanup_target(self.api,BUILD.run_id,BUILD.attempt,SHA)
        self.assertIn(identifier,self.api.releases)
        cleanup(self.api,BUILD.run_id,BUILD.attempt,'b'*40)
        self.assertIn(identifier,self.api.releases)

    def test_lost_publication_response_recovers_by_reading_state(self):
        identifier=self.complete();self.api.lost_publish_reply=True
        publish(self.api,self.directory,BUILD,identifier)
        self.assertFalse(self.api.releases[identifier]['draft'])
        self.assertEqual(sum(m=='PATCH' and b.get('draft') is False for m,e,b in self.api.mutations),1)

    def test_publication_can_resume_without_overwriting_repacked_zip(self):
        identifier=self.complete();publish(self.api,self.directory,BUILD,identifier)
        original=copy.deepcopy(self.api.assets[identifier]);before=len(self.api.mutations)
        (self.directory/'CHANGES.zip').write_text('new ZIP timestamps on rerun')
        retry=replace(BUILD,attempt=2)
        self.assertEqual(prepare(self.api,self.directory,retry),(identifier,True))
        publish(self.api,self.directory,retry,identifier)
        self.assertEqual(self.api.assets[identifier],original)
        self.assertEqual(len(self.api.mutations),before)
        with self.assertRaises(ReleaseError):upload(self.api,self.directory,retry,identifier)
        self.assertEqual(len(self.api.mutations),before)

    def test_incomplete_public_release_is_not_repaired_or_overwritten(self):
        identifier=self.complete();publish(self.api,self.directory,BUILD,identifier)
        self.api.assets[identifier].pop();before=len(self.api.mutations)
        with self.assertRaises(ReleaseError):self.prepared()
        self.assertEqual(len(self.api.mutations),before)

    def test_mirror_resume_revalidates_publication_without_local_zips(self):
        identifier=self.complete();publish(self.api,self.directory,BUILD,identifier)
        for file in self.directory.iterdir():file.unlink()
        before=len(self.api.mutations)
        verify_publication(self.api,identifier,TAG,SHA)
        self.assertEqual(len(self.api.mutations),before)
        self.api.assets[identifier].pop()
        with self.assertRaises(ReleaseError):verify_publication(self.api,identifier,TAG,SHA)
        self.assertEqual(len(self.api.mutations),before)

    def test_mirror_refuses_an_unpublished_or_different_source_release(self):
        identifier=self.complete()
        with self.assertRaises(ReleaseError):verify_publication(self.api,identifier,TAG,SHA)
        publish(self.api,self.directory,BUILD,identifier)
        with self.assertRaises(ReleaseError):verify_publication(self.api,identifier,TAG,'b'*40)

    def test_owner_changed_or_publication_happened_before_upload(self):
        for changed in ('public','owner'):
            self.api=FakeGitHub();identifier=self.prepared()
            if changed=='public':self.api.releases[identifier]['draft']=False
            else:self.api.releases[identifier]['body']=release_body('',replace(BUILD,attempt=2),self.expected)
            before=len(self.api.mutations)
            with self.assertRaises(ReleaseError):upload(self.api,self.directory,BUILD,identifier)
            self.assertEqual(len(self.api.mutations),before)

    def test_bad_or_duplicate_receipt_is_not_trusted(self):
        identifier=self.prepared();receipt=self.api.releases[identifier]['body']
        for body in (None, {}, [], 'manual','<!-- mfabd2-release-guard:v1 abc -->',receipt+receipt):
            with self.assertRaises(ReleaseError):read_receipt({'body':body})

    def test_tag_and_manifest_validation(self):
        for tag in ('../tag','x\ny','bad tag'):
            with self.assertRaises(ReleaseError):expected_assets(tag,'release')
        with self.assertRaises(ReleaseError):expected_assets(TAG,'wrong')
        with self.assertRaises(ReleaseError):replace(BUILD,source_sha='short')
        with self.assertRaises(ReleaseError):replace(BUILD,attempt=0)
        names=expected_assets(TAG,'ci')
        self.assertEqual(len(names),10)
        self.assertEqual(len([n for n in names if '-ci.apk' in n]),3)


class ApiAndWorkflowTests(unittest.TestCase):
    def test_listing_reads_all_pages(self):
        api=GitHub('example/test','unused');seen=[]
        def request(method,endpoint):
            seen.append(endpoint);return [{}]*(100 if endpoint.endswith('page=1') else 1)
        api.request=request
        self.assertEqual(len(api.listing('releases')),101)
        self.assertEqual(len(seen),2)

    def test_upload_streams_file_and_uses_api_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'test.zip';path.write_bytes(b'asset contents')
            def opening(request,timeout):
                self.assertTrue(hasattr(request.data,'read'))
                self.assertEqual(request.data.read(),b'asset contents')
                self.assertEqual(request.get_header('Content-length'),str(path.stat().st_size))
                self.assertEqual(request.get_header('X-github-api-version'),'2026-03-10')
                from io import BytesIO
                return BytesIO(b'{}')
            with patch('release_guard.urlopen',side_effect=opening):
                GitHub('example/test','unused').upload({'upload_url':'https://uploads.github.com/test{?name}'},path)
            with self.assertRaises(ReleaseError):
                GitHub('example/test','unused').upload({'upload_url':'https://other.example/test'},path)

    def test_invalid_json_response_is_an_explicit_api_error(self):
        from io import BytesIO
        with patch('release_guard.urlopen',return_value=BytesIO(b'{')):
            with self.assertRaisesRegex(ReleaseError,'invalid JSON'):
                GitHub('example/test','unused').request('PATCH','releases/1',{'draft':False})

    def test_cli_invalid_inputs_fail_before_network(self):
        env=dict(os.environ,GITHUB_REPOSITORY='example/test',GH_TOKEN='unused',RELEASE_TAG=TAG,
                 ANDROID_IDENTITY='release',SOURCE_SHA=SHA,RELEASE_PRERELEASE='bad',GITHUB_RUN_ID='519',GITHUB_RUN_ATTEMPT='1')
        result=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/release_guard.py'),'prepare'],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,1);self.assertIn('::error::',result.stderr)

    def test_publication_recovery_cleanup_and_mirror_are_separate(self):
        workflow=(ROOT/'.github/workflows/install-build.yml').read_text(encoding='utf-8')
        release=workflow.split('\n  release:\n',1)[1].split('\n  mirror:\n',1)[0]
        order=[release.index(x) for x in ('scripts/release_guard.py prepare','scripts/release_guard.py upload','scripts/release_guard.py publish')]
        self.assertEqual(order,sorted(order))
        self.assertNotIn('softprops/action-gh-release',release)
        self.assertIn('already_published',release)
        self.assertIn('if: failure() || cancelled()',release)
        self.assertIn('scripts/release_guard.py cleanup',release)
        self.assertIn('cancel-in-progress: false',release)
        self.assertNotIn('continue-on-error:',release)
        self.assertIn('needs: [meta, release]',workflow.split('\n  mirror:\n')[1])

    def test_cleanup_uses_trusted_default_branch_and_matching_tag_lock(self):
        workflow=(ROOT/'.github/workflows/release-cleanup.yml').read_text(encoding='utf-8')
        self.assertIn('workflow_run:',workflow)
        self.assertIn('workflows: [install]',workflow)
        self.assertIn('github.event.repository.default_branch',workflow)
        self.assertNotIn('ref: ${{ github.event.workflow_run.head_sha }}',workflow)
        self.assertIn('run_attempt',workflow)
        self.assertIn('release-${{ github.repository }}-${{ needs.discover.outputs.tag }}',workflow)
        self.assertIn('cleanup-completed',workflow)

    def test_android_has_no_release_upload_bypass(self):
        workflow=(ROOT/'.github/workflows/android.yml').read_text(encoding='utf-8')
        self.assertNotIn('gh release upload',workflow)
        self.assertNotIn('\n  publish:\n',workflow)
        self.assertIn('needs: validate_request',workflow)
        self.assertIn('Android 独立上传已禁用',workflow)

    def test_original_channels_and_asset_matrix_are_preserved(self):
        workflow=(ROOT/'.github/workflows/install-build.yml').read_text(encoding='utf-8')
        matrix=re.findall(r'- os: (\w+)\s+arch: (\w+)',workflow)
        self.assertEqual({f'{system}-{arch}' for system,arch in matrix},set(DESKTOP_TARGETS))
        policy="${{ contains(needs.meta.outputs.tag, '-beta') || contains(needs.meta.outputs.tag, '-alpha') || (contains(needs.meta.outputs.tag, '-ci') && !(github.event_name == 'workflow_dispatch' && github.event.inputs.ci_as_stable == 'true')) }}"
        self.assertIn('RELEASE_PRERELEASE: '+policy,workflow)
        self.assertIn('tag="${incremented_tag}-beta.${date_suffix}.${commit_short}"',workflow)
        entry=(ROOT/'.github/workflows/install.yml').read_text(encoding='utf-8')
        self.assertEqual(entry.count('python3 scripts/verify_release_guard.py'),1)
        self.assertNotIn('python3 scripts/verify_release_guard.py',workflow)


if __name__ == '__main__':
    unittest.main(verbosity=2)
