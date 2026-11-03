"""Trusted metadata-only dispatcher. Exported verbatim to the course repository."""

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import secrets
import urllib.error
import urllib.request
import urllib.parse
import zipfile


COURSE = "itmo-mla/spring-27"
GRADER = "itmo-mla/semester-grader"
CHECK = "ЛР1 / closed"
SHA = re.compile(r"[0-9a-f]{40}")
LAB = re.compile(r"students/([^/]+)/lab1/")
INFRA = "Сбой инфраструктуры, требуется повторный запуск"


class ApiError(Exception):
    def __init__(self, status):
        self.status = status


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, data=None):
        request = urllib.request.Request(
            "https://api.github.com" + path,
            data=None if data is None else json.dumps(data).encode(), method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code) from None
        return json.loads(body) if body else None

    def pages(self, path, key=None):
        for page in range(1, 101):
            separator = "&" if "?" in path else "?"
            data = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            items = data[key] if key else data
            yield from items
            if len(items) < 100:
                return
        raise ValueError("Pagination limit")

    def artifact(self, artifact_id):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_):
                return None
        request = urllib.request.Request(
            f"https://api.github.com/repos/{COURSE}/actions/artifacts/{artifact_id}/zip",
            headers={"Authorization": "Bearer " + self.token})
        try:
            urllib.request.build_opener(NoRedirect()).open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code != 302:
                raise ApiError(exc.code) from None
            location = exc.headers["Location"]
        else:
            raise ValueError("Artifact redirect required")
        parsed = urllib.parse.urlsplit(location)
        if (parsed.scheme != "https" or not parsed.hostname
                or not parsed.hostname.endswith((".blob.core.windows.net", ".githubusercontent.com"))):
            raise ValueError("Invalid artifact host")
        # The installation token is never forwarded to the storage host.
        with urllib.request.urlopen(location, timeout=30) as response:
            data = response.read(1024*1024+1)
        if len(data) > 1024*1024:
            raise ValueError("Artifact size limit")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != "dispatch-check.json" or entries[0].file_size > 4096:
                raise ValueError("Invalid dispatch artifact")
            return json.loads(archive.read(entries[0]))


def external_id(pr, sha, nonce):
    return f"lr1:{pr}:{sha}:{nonce}"


def check_identity(check, app_id, pr, sha, nonce):
    if (check["name"] != CHECK or check["head_sha"] != sha
            or str(check["app"]["id"]) != str(app_id)
            or check.get("external_id") != external_id(pr, sha, nonce)):
        raise ValueError("Check identity mismatch")


def finish(api, check_id, conclusion, summary):
    api.request("PATCH", f"/repos/{COURSE}/check-runs/{check_id}", {
        "status": "completed", "conclusion": conclusion,
        "output": {"title": CHECK, "summary": summary}})


def lab_directory(api, pr):
    if pr["changed_files"] > 3000:
        raise ValueError("Too many files")
    files = list(api.pages(f"/repos/{COURSE}/pulls/{pr['number']}/files"))
    if len(files) != pr["changed_files"]:
        raise ValueError("Incomplete file list")
    roots = set()
    for item in files:
        for name in (item["filename"], item.get("previous_filename", "")):
            match = LAB.match(name)
            if match:
                student = match[1]
                if student in (".", "..") or any(c in student for c in "\\\r\n\x00"):
                    raise ValueError("Invalid lab directory")
                roots.add(match[0].rstrip("/"))
    if len(roots) > 1:
        raise ValueError("Multiple labs")
    return next(iter(roots)) if roots else None


def dispatch(course, private, pr_number, sha):
    if type(pr_number) is not int or pr_number <= 0 or not SHA.fullmatch(sha):
        raise ValueError("Invalid request")
    pr = course.request("GET", f"/repos/{COURSE}/pulls/{pr_number}")
    if pr["base"]["repo"]["full_name"] != COURSE:
        raise ValueError("Invalid repository")
    if pr["state"] != "open" or pr["head"]["sha"] != sha:
        return
    invalid_lab = False
    try:
        directory = lab_directory(course, pr)
    except ValueError:
        directory, invalid_lab = None, True
    if not directory and not invalid_lab:
        return
    latest = course.request("GET", f"/repos/{COURSE}/pulls/{pr_number}")
    if latest["head"]["sha"] != sha or latest["state"] != "open":
        return
    nonce = secrets.token_hex(16)
    check = course.request("POST", f"/repos/{COURSE}/check-runs", {
        "name": CHECK, "head_sha": sha, "status": "queued",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "external_id": external_id(pr_number, sha, nonce),
        "output": {"title": CHECK, "summary": f"Проверка поставлена в очередь. Коммит `{sha}`."}})
    if os.environ.get("GITHUB_ACTIONS") == "true":
        Path("dispatch-check.json").write_text(json.dumps({"pr": pr_number, "sha": sha,
            "check_id": check["id"], "nonce": nonce}), encoding="utf-8")
    if invalid_lab:
        finish(course, check["id"], "failure", "В PR должен быть один каталог ЛР1. Проверьте состав изменений.")
        return
    try:
        private.request("POST", f"/repos/{GRADER}/actions/workflows/grade-lab1.yml/dispatches", {
            "ref": "main", "inputs": {"pr": str(pr_number), "sha": sha,
                                      "check_id": str(check["id"]), "nonce": nonce}})
    except Exception:
        finish(course, check["id"], "action_required", INFRA)
        raise


def recover(api, app_id, now=None):
    """Expired runs on recent pushes, including closed PRs and superseded SHAs."""
    now = now or datetime.now(timezone.utc)
    # A small public artifact retains the exact Check ID even after force pushes
    # or PR closure. It contains metadata only, never student output or tests.
    from datetime import timedelta
    since = (now-timedelta(days=7)).date().isoformat()
    for run in api.pages(f"/repos/{COURSE}/actions/workflows/closed-lab1.yml/runs?created=%3E%3D{since}", "workflow_runs"):
        for artifact in api.pages(f"/repos/{COURSE}/actions/runs/{run['id']}/artifacts", "artifacts"):
            if artifact["expired"] or not artifact["name"].startswith("lr1-dispatch-"):
                continue
            record = api.artifact(artifact["id"])
            check = api.request("GET", f"/repos/{COURSE}/check-runs/{int(record['check_id'])}")
            check_identity(check, app_id, int(record["pr"]), record["sha"], record["nonce"])
            if check["status"] != "completed":
                started = datetime.fromisoformat(check["started_at"].replace("Z", "+00:00"))
                if (now-started).total_seconds() > 7200:
                    finish(api, check["id"], "action_required", INFRA)
    for pr in api.pages(f"/repos/{COURSE}/pulls?state=all&sort=updated&direction=desc"):
        updated = datetime.fromisoformat(pr["updated_at"].replace("Z", "+00:00"))
        if (now - updated).total_seconds() > 7*86400:
            break
        commits = {item["sha"] for item in api.pages(f"/repos/{COURSE}/pulls/{pr['number']}/commits")}
        commits.add(pr["head"]["sha"])
        for sha in commits:
            for check in api.pages(f"/repos/{COURSE}/commits/{sha}/check-runs?filter=all", "check_runs"):
                identity = (check.get("external_id") or "").split(":")
                if (check["name"] != CHECK or str(check["app"]["id"]) != str(app_id)
                        or len(identity) != 4 or identity[:3] != ["lr1", str(pr["number"]), sha]
                        or check["status"] == "completed"):
                    continue
                created = datetime.fromisoformat(check["started_at"].replace("Z", "+00:00"))
                if (now-created).total_seconds() > 7200:
                    finish(api, check["id"], "action_required", INFRA)


def main():
    course = GitHub(os.environ["COURSE_TOKEN"])
    if os.environ.get("GITHUB_REPOSITORY") != COURSE:
        raise ValueError("Invalid repository")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if os.environ["GITHUB_EVENT_NAME"] in ("schedule", "workflow_dispatch"):
        if event.get("inputs", {}).get("pr"):
            number = int(event["inputs"]["pr"])
            pr = course.request("GET", f"/repos/{COURSE}/pulls/{number}")
            dispatch(course, GitHub(os.environ["DISPATCH_TOKEN"]), number, pr["head"]["sha"])
        else:
            recover(course, os.environ["GRADER_APP_ID"])
    else:
        dispatch(course, GitHub(os.environ["DISPATCH_TOKEN"]), event["number"], event["pull_request"]["head"]["sha"])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(INFRA)
        raise SystemExit(2)
