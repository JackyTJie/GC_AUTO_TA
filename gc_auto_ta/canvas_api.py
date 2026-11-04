"""Canvas 侧：读取提交、下载附件、回写分数与评论。

用 canvasapi（>=3.6）。要点（详见 docs/03、docs/06）：
    * 列提交：GET /courses/:cid/assignments/:aid/submissions（include[] 带 user / submission_comments）
    * 下载附件：Canvas 返回的 attachments 里每项都是 File 对象，file.download(path) 直接可用
    * 打分+评论：PUT /courses/:cid/assignments/:aid/submissions/:uid
      —— 需要 TA 角色有 manage grades 权限；没有就是 403
    * 批量：POST /courses/:cid/assignments/:aid/submissions/update_grades（异步，返回 Progress）
    * 匿名批改：要走 /anonymous_submissions/:anonymous_id（canvasapi 未封装，这里直发请求）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .util import RateLimiter, log, retry, write_json

SUBMISSION_INCLUDES = [
    "user",
    "submission_comments",
    "submission_history",
    "rubric_assessment",
]


@dataclass
class DownloadedSubmission:
    student_key: str
    canvas_user_id: int
    submission_type: str
    submitted_at: str | None
    files: list[Path]
    note: str = ""
    error_kind: str = ""


def html_to_text(html: str) -> str:
    """Canvas 的 body 是 HTML 片段；清成纯文本，便于喂给评分脚本/日志。"""
    if not html:
        return ""
    s = re.sub(r"(?s)<(script|style).*?</\1>", " ", html)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p\s*>", "\n\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    import html as _html
    s = _html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


class CanvasGateway:
    def __init__(self, base_url: str, token: str, course_id: int, assignment_id: int,
                 min_interval: float = 0.1, attachment_download_timeout: float = 5.0) -> None:
        try:
            from canvasapi import Canvas
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("需要 canvasapi：pip install -r requirements.txt") from exc

        self.canvas = Canvas(base_url, token)
        self.course_id = int(course_id)
        self.assignment_id = int(assignment_id)
        self.limiter = RateLimiter(min_interval)
        self.attachment_download_timeout = float(attachment_download_timeout)
        self._course = None
        self._assignment = None

    # ------------------------------------------------------------ 基础
    @property
    def course(self):
        if self._course is None:
            self._course = self.canvas.get_course(self.course_id)
        return self._course

    @property
    def assignment(self):
        if self._assignment is None:
            self._assignment = self.course.get_assignment(self.assignment_id)
        return self._assignment

    def me(self) -> dict:
        u = self.canvas.get_current_user()
        return {"id": getattr(u, "id", None), "name": getattr(u, "name", ""),
                "login_id": getattr(u, "login_id", "")}

    def assignment_props(self) -> dict:
        a = self.assignment
        return {
            "id": getattr(a, "id", None),
            "name": getattr(a, "name", ""),
            "points_possible": getattr(a, "points_possible", None),
            "grading_type": getattr(a, "grading_type", None),
            "published": getattr(a, "published", None),
            "due_at": getattr(a, "due_at", None),
            "anonymous_grading": getattr(a, "anonymous_grading", False),
            "submission_types": getattr(a, "submission_types", None),
        }

    def users_by_id(self) -> dict[int, Any]:
        """课程里所有学生（用于把 user_id 映射回 login_id / 姓名）。"""
        out: dict[int, Any] = {}
        try:
            for u in self.course.get_users(enrollment_type=["student"], per_page=100):
                out[int(u.id)] = u
        except Exception as exc:  # 权限不足时降级为匿名
            log().warning("读取课程学生列表失败（%s），将只能依赖提交里的 user_name/user_login_id", exc)
        return out

    # ------------------------------------------------------------ 读提交
    def submissions(self) -> list[Any]:
        def _call():
            self.limiter.wait()
            return list(self.assignment.get_submissions(include=SUBMISSION_INCLUDES))

        subs = retry(_call, attempts=4, base=1.0, logger=log())
        log().info("Canvas 提交数：%d", len(subs))
        return subs

    def download(self, submission: Any, out_root: Path, student_key: str) -> DownloadedSubmission:
        """把一份提交落到本地目录。附件按原名保存，文本提交存成 solution.txt。"""
        dest = Path(out_root) / student_key
        dest.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        stype = str(getattr(submission, "submission_type", "") or "none")
        note = ""
        error_kind = ""
        try:
            if stype == "online_upload":
                for att in getattr(submission, "attachments", []) or []:
                    name = getattr(att, "display_name", None) or getattr(att, "filename", "file")
                    path = dest / str(name)
                    if not path.exists():
                        self.limiter.wait()
                        retry(lambda a=att, p=path: self._download_attachment(a, p),
                              attempts=3, base=1.5, logger=log())
                    files.append(path)
            elif stype == "online_text_entry":
                text = html_to_text(getattr(submission, "body", "") or "")
                path = dest / "solution.txt"
                path.write_text(text, encoding="utf-8")
                files.append(path)
            elif stype == "online_url":
                path = dest / "url.txt"
                path.write_text(str(getattr(submission, "url", "")), encoding="utf-8")
                files.append(path)
            elif stype in ("", "none", None):
                note = "未提交"
            else:
                note = f"暂不支持的提交类型：{stype}（quiz / media / LTI 需人工处理）"
        except Exception as exc:
            note = f"下载失败：{exc}"
            error_kind = "system"
            log().warning("学生 %s 下载失败：%s", student_key, exc)

        write_json(dest / "_canvas.json", {
            "student_key": student_key,
            "user_id": getattr(submission, "user_id", None),
            "submission_type": stype,
            "submitted_at": getattr(submission, "submitted_at", None),
            "attempt": getattr(submission, "attempt", None),
            "late": getattr(submission, "late", None),
            "score": getattr(submission, "score", None),
            "grade": getattr(submission, "grade", None),
            "workflow_state": getattr(submission, "workflow_state", None),
            "files": [f.name for f in files],
            "note": note,
        })
        return DownloadedSubmission(
            student_key=student_key,
            canvas_user_id=int(getattr(submission, "user_id", 0) or 0),
            submission_type=stype,
            submitted_at=getattr(submission, "submitted_at", None),
            files=files,
            note=note,
            error_kind=error_kind,
        )

    def _download_attachment(self, attachment: Any, path: Path) -> None:
        """Download one attachment with a timeout, preserving canvasapi error handling."""
        requester = getattr(attachment, "_requester", None)
        session = getattr(requester, "_session", None)
        if (session is None or not hasattr(session, "get") or
                not hasattr(requester, "_get_request")):
            raise RuntimeError("Cannot apply attachment timeout: Canvas requester session unavailable")

        original_get = requester._get_request

        def timed_get(url, headers=None, params=None, **kwargs):
            return session.get(
                url,
                headers=headers,
                params=params,
                timeout=self.attachment_download_timeout,
            )

        requester._get_request = timed_get
        try:
            attachment.download(str(path))
        finally:
            requester._get_request = original_get

    # ------------------------------------------------------------ 写回
    def post_grade(self, canvas_user_id: int, posted_grade: str | float,
                   comment: str = "", dry_run: bool = True,
                   anonymous_id: str | None = None) -> dict:
        """给单个学生打分 + 评论。dry_run=True 时只打印，不落库。"""
        grade = str(posted_grade)
        payload = {"submission": {"posted_grade": grade}}
        payload_comment = {"text_comment": comment} if comment else None
        if dry_run:
            log().info("[dry-run] user=%s grade=%s comment=%d 字",
                       canvas_user_id, grade, len(comment or ""))
            return {"dry_run": True, "user_id": canvas_user_id, "posted_grade": grade}

        self.limiter.wait()

        def _call():
            if anonymous_id:
                kwargs: dict[str, Any] = {"submission": {"posted_grade": grade}}
                if comment:
                    kwargs["comment"] = {"text_comment": comment}
                resp = self.assignment._requester.request(
                    "PUT",
                    f"courses/{self.course_id}/assignments/{self.assignment_id}"
                    f"/anonymous_submissions/{anonymous_id}",
                    _kwargs=kwargs,
                )
                return resp.json()
            sub = self.assignment.get_submission(canvas_user_id)
            kwargs = dict(payload)
            if payload_comment:
                kwargs["comment"] = payload_comment
            sub.edit(**kwargs)
            return {"user_id": canvas_user_id, "posted_grade": grade}

        return retry(_call, attempts=4, base=2.0, logger=log())

    def post_grades_bulk(self, grades: dict[int, dict], dry_run: bool = True,
                         poll_interval: float = 2.0, poll_timeout: float = 600.0) -> dict:
        """批量回写（异步任务）。grades: {canvas_user_id: {"posted_grade":..., "text_comment":...}}"""
        import time
        if dry_run:
            for uid, g in grades.items():
                log().info("[dry-run][bulk] user=%s grade=%s", uid, g.get("posted_grade"))
            return {"dry_run": True, "count": len(grades)}

        self.limiter.wait()
        progress = retry(lambda: self.assignment.submissions_bulk_update(grade_data=grades),
                         attempts=3, base=2.0, logger=log())
        deadline = time.monotonic() + poll_timeout
        state = "queued"
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            p = progress.query()
            state = getattr(p, "workflow_state", "unknown")
            if state in ("completed", "failed"):
                break
        log().info("批量回写结束：%s（%d 条）", state, len(grades))
        return {"workflow_state": state, "count": len(grades), "progress_id": getattr(progress, "id", None)}

    def gradeable_anonymous_ids(self) -> dict[int, str]:
        """匿名批改作业：取 {canvas_user_id: anonymous_id}（尽力而为，失败返回空）。"""
        out: dict[int, str] = {}
        try:
            self.limiter.wait()
            url = (f"/api/v1/courses/{self.course_id}/assignments/{self.assignment_id}"
                   f"/gradeable_students?include[]=anonymous_ids&per_page=100")
            data = self.assignment._requester.request("GET", url, _url=url).json()
            for item in data or []:
                uid = item.get("id")
                anon = item.get("anonymous_id") or (item.get("anonymous_ids") or [None])[0]
                if uid is not None and anon:
                    out[int(uid)] = str(anon)
        except Exception as exc:
            log().warning("获取匿名 id 失败：%s", exc)
        return out
