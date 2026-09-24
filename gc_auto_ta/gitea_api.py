"""Gitea REST 客户端（对齐本机 Gitea 1.23.6 的 swagger）。

用到的端点（均已在本机 /swagger.v1.json 中核对存在）：
    GET    /api/v1/version
    GET    /api/v1/orgs/{org}
    POST   /api/v1/orgs/{org}/repos                 建仓
    GET    /api/v1/repos/{owner}/{repo}
    PATCH  /api/v1/repos/{owner}/{repo}             改默认分支等
    GET    /api/v1/orgs/{org}/teams
    POST   /api/v1/orgs/{org}/teams                 建 Team
    PUT    /api/v1/teams/{id}/members/{username}
    PUT    /api/v1/teams/{id}/repos/{org}/{repo}
    GET    /api/v1/orgs/{org}/actions/secrets
    PUT    /api/v1/orgs/{org}/actions/secrets/{name} 写 org secret（body: {"data": "..."}）
    GET    /api/v1/orgs/{org}/actions/runners/registration-token
    GET    /api/v1/repos/{owner}/{repo}/actions/tasks
    GET    /api/v1/repos/{owner}/{repo}/contents/{filepath}?ref=...
    POST   /api/v1/repos/{owner}/{repo}/issues
    GET    /api/v1/repos/{owner}/{repo}/issues?state=all

注意：1.23.6 没有 workflow dispatch 接口，也没有 runs 列表接口 ——
触发只能靠 push（重跑用空提交），状态只能靠 /actions/tasks。
"""

from __future__ import annotations

import base64
from typing import Any

from .util import RateLimiter, log, retry


class GiteaError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: str) -> None:
        super().__init__(f"Gitea {method} {path} → HTTP {status}: {body[:300]}")
        self.status = status
        self.method = method
        self.path = path
        self.body = body


class GiteaClient:
    def __init__(self, url: str, token: str, timeout: float = 30.0,
                 min_interval: float = 0.05) -> None:
        import requests  # 延迟导入，doctor 阶段好给友好报错
        self.base = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.limiter = RateLimiter(min_interval)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ------------------------------------------------------------ 底层
    def request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None,
                ok_statuses: tuple[int, ...] = (200, 201, 204), raise_on_error: bool = True,
                raw: bool = False):
        self.limiter.wait()
        url = path if path.startswith("http") else f"{self.base}/api/v1{path}"
        resp = self.session.request(method, url, json=json_body, params=params, timeout=self.timeout)
        if resp.status_code not in ok_statuses:
            if raise_on_error:
                raise GiteaError(resp.status_code, method, path, resp.text)
            return None
        if raw:
            return resp
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ------------------------------------------------------------ 探测
    def version(self) -> str:
        return str(self.request("GET", "/version").get("version"))

    def get_org(self, org: str) -> dict | None:
        return self.request("GET", f"/orgs/{org}", raise_on_error=False,
                            ok_statuses=(200,))

    def whoami(self) -> dict:
        return self.request("GET", "/user")

    def registration_token(self, org: str) -> str | None:
        """组织级 runner 注册令牌；能拿到说明 PAT 对该组织有 admin 权（doctor 用它探权限）。"""
        data = self.request("GET", f"/orgs/{org}/actions/runners/registration-token",
                            raise_on_error=False, ok_statuses=(200,))
        return (data or {}).get("token")

    def list_secrets(self, org: str) -> list[str]:
        data = self.request("GET", f"/orgs/{org}/actions/secrets", raise_on_error=False,
                            ok_statuses=(200,)) or []
        if isinstance(data, dict):
            data = data.get("secrets", [])
        return [s.get("name", "") for s in data]

    def set_org_secret(self, org: str, name: str, value: str) -> bool:
        self.request("PUT", f"/orgs/{org}/actions/secrets/{name}", json_body={"data": value})
        log().info("org secret 已写入：%s/%s", org, name)
        return True

    # ------------------------------------------------------------ 仓库
    def get_repo(self, owner: str, repo: str) -> dict | None:
        return self.request("GET", f"/repos/{owner}/{repo}", raise_on_error=False,
                            ok_statuses=(200,))

    def create_repo(self, org: str, name: str, private: bool = True, auto_init: bool = False,
                    default_branch: str = "main", description: str = "") -> dict:
        body = {
            "name": name,
            "private": private,
            "auto_init": auto_init,
            "default_branch": default_branch,
            "description": description,
        }
        return self.request("POST", f"/orgs/{org}/repos", json_body=body,
                            ok_statuses=(201, 409))

    def ensure_repo(self, org: str, name: str, private: bool = True,
                    default_branch: str = "main", description: str = "") -> tuple[dict, bool]:
        """幂等建仓：已存在就直接返回。返回 (repo, created)。"""
        existing = self.get_repo(org, name)
        if existing:
            return existing, False
        created = self.create_repo(org, name, private=private, default_branch=default_branch,
                                   description=description)
        if created is None:  # 409 之类：并发下别人刚建
            again = self.get_repo(org, name)
            if again:
                return again, False
        return created or {}, True

    def set_default_branch(self, owner: str, repo: str, branch: str) -> None:
        self.request("PATCH", f"/repos/{owner}/{repo}", json_body={"default_branch": branch},
                     ok_statuses=(200, 201))

    def delete_repo(self, owner: str, repo: str) -> None:
        self.request("DELETE", f"/repos/{owner}/{repo}", ok_statuses=(204,))

    # ------------------------------------------------------------ Team
    def list_teams(self, org: str) -> list[dict]:
        return self.request("GET", f"/orgs/{org}/teams") or []

    def ensure_team(self, org: str, name: str, permission: str = "write",
                    description: str = "", can_create_org_repo: bool = False) -> dict:
        for t in self.list_teams(org):
            if t.get("name") == name:
                return t
        body = {
            "name": name,
            "description": description or name,
            "permission": permission,   # read | write | admin
            "can_create_org_repo": can_create_org_repo,
            "units_map": {"repo.code": permission, "repo.issues": "write"},
        }
        return self.request("POST", f"/orgs/{org}/teams", json_body=body, ok_statuses=(201,))

    def add_team_member(self, team_id: int, username: str) -> None:
        self.request("PUT", f"/teams/{team_id}/members/{username}", ok_statuses=(204, 200))

    def add_team_repo(self, team_id: int, org: str, repo: str) -> None:
        self.request("PUT", f"/teams/{team_id}/repos/{org}/{repo}", ok_statuses=(204, 200))

    # ------------------------------------------------------------ Actions / 内容
    def list_tasks(self, owner: str, repo: str) -> list[dict]:
        data = self.request("GET", f"/repos/{owner}/{repo}/actions/tasks", raise_on_error=False,
                            ok_statuses=(200,)) or {}
        if isinstance(data, dict):
            return list(data.get("workflow_runs", []) or data.get("tasks", []) or [])
        return list(data)

    def get_file(self, owner: str, repo: str, path: str, ref: str | None = None) -> str | None:
        """读取仓库里一个文件的内容（base64 → text）。不存在返回 None。"""
        params = {"ref": ref} if ref else None
        data = self.request("GET", f"/repos/{owner}/{repo}/contents/{path}", params=params,
                            raise_on_error=False, ok_statuses=(200,))
        if not data or isinstance(data, list):
            return None
        content = data.get("content")
        if not content:
            return None
        try:
            return base64.b64decode(content).decode("utf-8", errors="replace")
        except Exception:
            return None

    def create_issue(self, owner: str, repo: str, title: str, body: str) -> dict:
        return self.request("POST", f"/repos/{owner}/{repo}/issues",
                            json_body={"title": title, "body": body}, ok_statuses=(201,))

    def list_issues(self, owner: str, repo: str, state: str = "all") -> list[dict]:
        data = self.request("GET", f"/repos/{owner}/{repo}/issues", params={"state": state,
                                                                            "limit": 50},
                            raise_on_error=False, ok_statuses=(200,)) or []
        return [i for i in data if "pull_request" not in i]

    # ------------------------------------------------------------ 长任务
    def wait_task(self, owner: str, repo: str, head_sha: str | None = None,
                  poll_interval: float = 10.0, timeout: float = 1800.0) -> dict | None:
        """轮询 /actions/tasks，等与 head_sha 匹配的最新任务跑完。

        返回最后看到的任务 dict（含 status 字段）；超时返回 None。
        """
        import time
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            try:
                tasks = retry(lambda: self.list_tasks(owner, repo), attempts=3, base=2.0, logger=log())
            except Exception as exc:
                log().warning("读取 tasks 失败：%s", exc)
                tasks = []
            cand = [t for t in tasks if not head_sha or (t.get("head_sha") or "").startswith(head_sha[:7])]
            if not cand:
                cand = tasks
            if cand:
                last = sorted(cand, key=lambda t: t.get("run_number") or t.get("id") or 0)[-1]
                status = str(last.get("status", "")).lower()
                if status in ("success", "failure", "failed", "cancelled", "error", "skipped"):
                    return last
                log().debug("任务 %s 状态：%s", last.get("id"), status)
            time.sleep(poll_interval)
        return last

    def clone_url(self, owner: str, repo: str, use_internal: bool = False,
                  internal_url: str | None = None) -> str:
        base = (internal_url or self.base) if use_internal else self.base
        return f"{base.rstrip('/')}/{owner}/{repo}.git"
