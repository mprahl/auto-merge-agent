import base64
import json

import httpx
import pytest

from auto_merge_agent.github import GitHubError, GitHubForge
from auto_merge_agent.types import ChangedFile, Decision, PullRequestRef, PullRequestSnapshot


@pytest.mark.asyncio
async def test_complete_diff_is_pinned_and_recovers_only_matching_text_files():
    requests = []
    diff = "diff --git a/main.go b/main.go\n--- a/main.go\n+++ b/main.go\n@@ -1 +1,2 @@\n-old\n+new\n+extra\n"
    diff += "diff --git a/image.png b/image.png\nBinary files a/image.png and b/image.png differ\n"
    diff += "diff --git a/script.sh b/script.sh\nold mode 100644\nnew mode 100755\n"

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=diff)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        forge = GitHubForge(token="test", client=client)
        pr = PullRequestSnapshot(ref=PullRequestRef(owner="acme", repo="repo", number=1),
                                 head_sha="a" * 40, base_sha="b" * 40, state="open", draft=False,
                                 title="Test", url="https://example.com", changed_files=2)
        original = [ChangedFile(path="main.go", status="modified", additions=2, deletions=1),
                    ChangedFile(path="image.png", status="modified", additions=0, deletions=0),
                    ChangedFile(path="script.sh", status="modified", additions=0, deletions=0)]
        files = await forge.get_complete_changed_files(pr, original)
        mismatched = await forge.get_complete_changed_files(pr, [original[0].model_copy(update={"additions": 3})])
    assert requests[0].url.path == f"/repos/acme/repo/compare/{pr.base_sha}...{pr.head_sha}"
    assert requests[0].headers["Accept"] == "application/vnd.github.diff"
    assert files[0].has_complete_patch
    assert "+extra" in files[0].patch
    assert not files[1].has_complete_patch
    assert files[2].has_complete_patch
    assert "new mode 100755" in files[2].diff_metadata
    assert not mismatched[0].has_complete_patch


@pytest.mark.asyncio
async def test_changed_files_preserve_rename_source():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[{
        "filename": "docs/new.md", "previous_filename": "src/app.py", "status": "renamed",
        "additions": 10, "deletions": 5,
    }]))) as client:
        forge = GitHubForge(token="test", client=client)
        pr = PullRequestSnapshot(ref=PullRequestRef(owner="acme", repo="repo", number=1),
                                 head_sha="a" * 40, base_sha="b" * 40, state="open", draft=False,
                                 title="Test", url="https://example.com", changed_files=1)
        files = await forge.get_changed_files(pr)
    assert files[0].previous_path == "src/app.py"


@pytest.mark.parametrize("status_collision", [False, True])
@pytest.mark.parametrize("new_status, new_started", [("queued", None), ("in_progress", "2026-10-09T19:00:00Z")])
@pytest.mark.asyncio
async def test_new_check_run_supersedes_older_success(new_status, new_started, status_collision):
    def handler(request):
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [
                {"id": 2, "name": "build", "app": {"slug": "github-actions"},
                 "status": new_status, "conclusion": None, "started_at": new_started},
                {"id": 1, "name": "build", "app": {"slug": "github-actions"},
                 "status": "completed", "conclusion": "success", "completed_at": "2026-10-09T20:00:00Z"},
            ]})
        statuses = [{"context": "build", "creator": {"login": "github-actions"}, "state": "success"}] if status_collision else []
        return httpx.Response(200, json=statuses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        forge = GitHubForge(token="test", client=client)
        pr = PullRequestSnapshot(ref=PullRequestRef(owner="acme", repo="repo", number=1),
                                 head_sha="a" * 40, base_sha="b" * 40, state="open", draft=False,
                                 title="Test", url="https://example.com", changed_files=1)
        checks = await forge.get_checks(pr)
    assert len(checks) == (2 if status_collision else 1)
    assert checks[0].status == new_status
    assert checks[0].conclusion is None


def test_comment_marker_does_not_establish_bot_ownership():
    forge = GitHubForge(app_id="123")
    marker = "<!-- auto-merge-agent decision -->"
    comment = {"body": marker, "user": {"login": "unrelated[bot]", "type": "Bot"},
               "performed_via_github_app": {"id": 456}}
    assert not forge._own_comment(comment, marker)
    comment["performed_via_github_app"]["id"] = 123
    assert forge._own_comment(comment, marker)


@pytest.mark.asyncio
async def test_repository_context_is_read_from_base_commit():
    requested = []

    def handler(request):
        requested.append(request)
        if request.url.path.endswith("/AGENTS.md"):
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "size": 11,
                                             "content": base64.b64encode(b"Use caution").decode()})
        return httpx.Response(404, json={"message": "Not Found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha="a" * 40, base_sha="b" * 40,
                             state="open", draft=False, title="Test", url="https://example.com", changed_files=1)
    try:
        assert await forge.get_repository_file(pr, "AGENTS.md") == "Use caution"
        assert await forge.get_repository_file(pr, "missing.md") is None
    finally:
        await client.aclose()
    assert requested[0].url.params["ref"] == "b" * 40


@pytest.mark.asyncio
async def test_merge_request_is_guarded_by_evaluated_head():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"merged": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha="a" * 40, base_sha="b" * 40, state="open", draft=False, title="Test", url="https://github.test/acme/repo/pull/12", changed_files=1)
    try:
        assert await forge.merge(pr, "squash")
    finally:
        await client.aclose()
    assert requests[0].url.path == "/repos/acme/repo/pulls/12/merge"
    assert json.loads(requests[0].content) == {"sha": "a" * 40, "merge_method": "squash"}


@pytest.mark.asyncio
async def test_changed_head_rejects_merge():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(409, json={"message": "Head branch was modified"})))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha="a" * 40, base_sha="b" * 40, state="open", draft=False, title="Test", url="https://github.test/acme/repo/pull/12", changed_files=1)
    try:
        assert not await forge.merge(pr, "squash")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_error_messages_exclude_response_bodies():
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(403, json={"message": "sensitive repository content"})))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha="a" * 40, base_sha="b" * 40,
                             state="open", draft=False, title="Test", url="https://example.com", changed_files=1)
    try:
        with pytest.raises(GitHubError, match="returned 403") as exc:
            await forge.merge(pr, "squash")
        assert "sensitive repository content" not in str(exc.value)
        assert exc.value.status_code == 403
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_missing_app_installation_has_actionable_error(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404, json={"message": "Not Found"})))
    forge = GitHubForge(app_id="1", private_key="test", api_url="https://api.github.test", client=client)
    monkeypatch.setattr(forge, "_app_jwt", lambda: "test-jwt")
    try:
        with pytest.raises(GitHubError, match="not installed on acme/repo") as exc:
            await forge._token(PullRequestRef(owner="acme", repo="repo", number=12))
        assert exc.value.status_code == 404
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pr_checks_include_test_merge_commit():
    head, merge = "a" * 40, "c" * 40

    def handler(request):
        if request.url.path.endswith(f"/{merge}/check-runs"):
            return httpx.Response(200, json={"check_runs": [{
                "id": 1, "name": "demo-ci", "app": {"slug": "github-actions"},
                "status": "completed", "conclusion": "success", "completed_at": "2026-10-09T20:00:00Z",
            }]})
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if request.url.path.endswith("/statuses"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha=head, base_sha="b" * 40, merge_commit_sha=merge,
                             state="open", draft=False, title="Test", url="https://example.com", changed_files=1)
    try:
        checks = await forge.get_checks(pr)
    finally:
        await client.aclose()
    assert [(check.name, check.source, check.sha) for check in checks] == [("demo-ci", "github-actions", merge)]


@pytest.mark.asyncio
async def test_merge_checklist_is_one_comment_updated_after_merge():
    comments = []
    methods = []

    def handler(request):
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=comments)
        body = json.loads(request.content)["body"]
        if request.method == "POST":
            comments.append({"id": 7, "body": body, "user": {"login": "auto-merge-agent[bot]"}})
            return httpx.Response(201, json=comments[0])
        if request.method == "PATCH":
            comments[0]["body"] = body
            return httpx.Response(200, json=comments[0])
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha="a" * 40, base_sha="b" * 40,
                             state="open", draft=False, title="Test", url="https://example.com", changed_files=1)
    decision = Decision(state="eligible", head_sha=pr.head_sha, reasons=["Eligible"],
                        config_revision="abc123", passed_checks=["CI lint passed", "CodeRabbit reviewed current HEAD"])
    try:
        await forge.publish_merge_checklist(pr, decision, "attempting")
        await forge.publish_merge_checklist(pr, decision, "merged")
        await forge.publish_merge_checklist(pr, decision, "merged")
    finally:
        await client.aclose()
    assert methods.count("POST") == 1
    assert methods.count("PATCH") == 1
    assert "- [x] CI lint passed" in comments[0]["body"]
    assert "Auto-merged after these checks passed" in comments[0]["body"]


@pytest.mark.asyncio
async def test_decision_discovers_bot_login_and_reuses_app_output():
    head = "a" * 40
    comments = []
    runs = []
    writes = []

    def handler(request):
        path = request.url.path
        if request.method == "GET" and path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": runs})
        if request.method == "GET" and path.endswith("/comments"):
            return httpx.Response(200, json=comments)
        data = json.loads(request.content)
        writes.append((request.method, path))
        if "/check-runs" in path:
            if request.method == "POST":
                runs.append({**data, "id": len(runs) + 1, "app": {"id": 123, "slug": "real-app"}})
            else:
                runs[0].update(data)
            return httpx.Response(200, json=runs[-1])
        if request.method == "POST":
            comments.append({"id": 7, "body": data["body"], "user": {"login": "real-app[bot]", "type": "Bot"}})
        else:
            comments[0]["body"] = data["body"]
        return httpx.Response(200, json=comments[0])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    forge = GitHubForge(app_id="123", token="test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha=head, base_sha="b" * 40,
                             state="open", draft=False, title="Test", url="https://example.com", changed_files=1)
    decision = Decision(state="human_review", head_sha=head, reasons=["CI failed"], config_revision="abc123")
    try:
        assert forge.bot_login is None
        await forge.publish_decision(pr, decision, [], True)
        assert forge.bot_login == "real-app[bot]"
        assert len(writes) == 2
        await forge.publish_decision(pr, decision, [], True)
        assert len(writes) == 2
        changed = decision.model_copy(update={"reasons": ["CI passed; review needed"]})
        await forge.publish_decision(pr, changed, [], True)
        assert writes[-2][0] == "PATCH"
        assert writes[-1][0] == "PATCH"
        assert len(comments) == len(runs) == 1
        new_head = "c" * 40
        await forge.publish_decision(
            pr.model_copy(update={"head_sha": new_head}),
            changed.model_copy(update={"head_sha": new_head}), [], True,
        )
        assert writes[-2][0] == "POST"
        assert writes[-1][0] == "PATCH"
        assert len(comments) == 1
        assert len(runs) == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_token_decision_uses_status_and_reuses_own_comment():
    head = "a" * 40
    statuses = []
    comments = [{"id": 7, "body": "<!-- auto-merge-agent decision -->\nOld",
                 "user": {"login": "maintainer", "type": "User"}}]
    writes = []

    def handler(request):
        path = request.url.path
        if path == "/user":
            return httpx.Response(200, json={"login": "maintainer"})
        if request.method == "GET":
            return httpx.Response(200, json=comments if path.endswith("/comments") else statuses)
        payload = json.loads(request.content)
        writes.append((request.method, path, payload))
        if "/statuses/" in path:
            statuses.insert(0, {**payload, "creator": {"login": "maintainer"}})
            return httpx.Response(201, json=statuses[0])
        comments[0]["body"] = payload["body"]
        return httpx.Response(200, json=comments[0])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha=head, base_sha="b" * 40,
                             state="open", draft=False, title="Test", url="https://example.com/pr/12", changed_files=1)
    decision = Decision(state="human_review", head_sha=head, reasons=["CI failed"], config_revision="abc123")
    try:
        await forge.publish_decision(pr, decision, [], True)
        await forge.publish_decision(pr, decision, [], True)
    finally:
        await client.aclose()
    assert len(writes) == 2
    assert writes[0] == ("POST", f"/repos/acme/repo/statuses/{head}", {
        "state": "failure", "context": "automerge/policy", "description": "CI failed",
        "target_url": pr.url,
    })
    assert writes[1][0:2] == ("PATCH", "/repos/acme/repo/issues/comments/7")
    assert "/check-runs" not in [path for _, path, _ in writes]


@pytest.mark.asyncio
async def test_token_status_replaces_changed_decision_and_preserves_other_users_status():
    head = "a" * 40
    statuses = [{"context": "automerge/policy", "state": "pending", "description": "Old",
                 "target_url": "https://example.com/pr/12", "creator": {"login": "someone-else"}}]
    posted = []

    def handler(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "maintainer"})
        if request.method == "GET":
            return httpx.Response(200, json=statuses)
        payload = json.loads(request.content)
        posted.append(payload)
        statuses.insert(0, {**payload, "creator": {"login": "maintainer"}})
        return httpx.Response(201, json=statuses[0])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    forge = GitHubForge(token="test", api_url="https://api.github.test", client=client)
    ref = PullRequestRef(owner="acme", repo="repo", number=12)
    pr = PullRequestSnapshot(ref=ref, head_sha=head, base_sha="b" * 40,
                             state="open", draft=False, title="Test", url="https://example.com/pr/12", changed_files=1)
    try:
        for state in ("waiting", "eligible", "eligible"):
            await forge.publish_decision(pr, Decision(state=state, head_sha=head,
                reasons=["Policy " + state], config_revision="abc123"), [], False)
    finally:
        await client.aclose()
    assert [item["state"] for item in posted] == ["pending", "success"]
