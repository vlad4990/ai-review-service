import os
import re
import json
import time
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Header, Request, HTTPException
from pydantic import BaseModel

app = FastAPI()

GITLAB_URL = os.environ["GITLAB_URL"].rstrip("/")
GITLAB_TOKEN = os.environ["GITLAB_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

# Anthropic / Claude Opus 4.5 via Messages API
ANTHROPIC_API_KEY = os.environ["OPUS_API_KEY"]  # оставим имя переменной как было
ANTHROPIC_API_URL = os.getenv("OPUS_API_URL", "https://api.anthropic.com/v1/messages")
ANTHROPIC_VERSION = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
OPUS_MODEL = os.getenv("OPUS_MODEL", "claude-opus-4-5")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "35840"))

MAX_DIFF_CHARS = int(os.getenv("MAX_DIFF_CHARS", "20000"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "60"))
SEM = asyncio.Semaphore(int(os.getenv("CONCURRENCY", "2")))

# Кэш обработанных SHA (project_id:mr_iid:sha -> timestamp)
PROCESSED_COMMITS: Dict[str, float] = {}
CACHE_TTL = 3600  # 1 час


def gl_headers() -> Dict[str, str]:
    return {"PRIVATE-TOKEN": GITLAB_TOKEN}


class ReviewItem(BaseModel):
    path: str
    line: int
    severity: str = "medium"
    comment: str


def extract_project_id(payload: Dict[str, Any]) -> int:
    proj = payload.get("project") or {}
    pid = proj.get("id")
    if pid is None:
        raise ValueError("No project.id in payload")
    return int(pid)


def extract_mr_iid(payload: Dict[str, Any]) -> int:
    obj = payload.get("object_attributes") or {}
    iid = obj.get("iid")
    if iid is None:
        raise ValueError("No object_attributes.iid in payload")
    return int(iid)


async def gitlab_get(client: httpx.AsyncClient, path: str) -> Any:
    url = f"{GITLAB_URL}{path}"
    r = await client.get(url, headers=gl_headers(), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


async def gitlab_post(client: httpx.AsyncClient, path: str, json_body: Dict[str, Any]) -> Any:
    url = f"{GITLAB_URL}{path}"
    r = await client.post(url, headers=gl_headers(), json=json_body, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


async def get_existing_discussions(
    client: httpx.AsyncClient, project_id: int, mr_iid: int
) -> List[Dict[str, Any]]:
    """Получить все существующие discussions для MR"""
    discussions = await gitlab_get(
        client, f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions"
    )
    return discussions or []


async def get_mr_diff_refs_and_changes(
    client: httpx.AsyncClient, project_id: int, mr_iid: int
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    mr = await gitlab_get(client, f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}")
    diff_refs = mr.get("diff_refs") or {}
    changes = await gitlab_get(client, f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/changes")
    files = changes.get("changes") or []
    return diff_refs, files


async def get_commit_diff(
    client: httpx.AsyncClient, project_id: int, commit_sha: str
) -> List[Dict[str, Any]]:
    """Получить diff только для конкретного коммита"""
    commit = await gitlab_get(client, f"/api/v4/projects/{project_id}/repository/commits/{commit_sha}")
    diffs = await gitlab_get(client, f"/api/v4/projects/{project_id}/repository/commits/{commit_sha}/diff")
    return diffs or []


def build_diff_text(files: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for f in files:
        new_path = f.get("new_path") or f.get("old_path") or "unknown"
        diff = f.get("diff") or ""
        if not diff:
            continue
        parts.append(f"FILE: {new_path}\n{diff}")
    return ("\n\n".join(parts))[:MAX_DIFF_CHARS]


def review_instructions(diff_text: str) -> str:
    # Важно: просим СТРОГО JSON массив, без markdown
    return f"""
Ты — senior software engineer. Проведи code review только по изменениям в diff.

Верни СТРОГО JSON массив (без markdown, без пояснений).
Формат каждого элемента:
{{
  "path": "relative/file/path",
  "line": <номер строки В НОВОМ файле>,
  "severity": "low|medium|high",
  "comment": "краткий комментарий"
}}

Правила:
- Используй только строки, которые реально существуют в НОВОЙ версии файла.
- Если не уверен в номере строки — НЕ добавляй элемент.
- Не дублируй одно и то же замечание.

DIFF:
{diff_text}
""".strip()


async def call_opus_anthropic(client: httpx.AsyncClient, diff_text: str) -> List[ReviewItem]:
    payload = {
        "model": OPUS_MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": review_instructions(diff_text)}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    r = await client.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    # Anthropic Messages API: content = [{"type":"text","text":"..."}]
    blocks = data.get("content") or []
    text = ""
    for b in blocks:
        if b.get("type") == "text":
            text += b.get("text", "")
    text = text.strip()

    # Достаём JSON массив (на случай если модель добавит что-то вокруг)
    m = re.search(r"\[.*\]", text, flags=re.S)
    if m:
        text = m.group(0)

    raw = json.loads(text)
    items: List[ReviewItem] = []
    for it in raw:
        try:
            items.append(ReviewItem(**it))
        except Exception:
            continue
    return items


def line_in_diff(file_diff: str, target_new_line: int) -> bool:
    # Hunk header: @@ -a,b +c,d @@
    for h in re.finditer(r"@@\s*-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s*@@", file_diff):
        start = int(h.group(1))
        count = int(h.group(2) or "1")
        if start <= target_new_line <= (start + max(count - 1, 0)):
            return True
    return False


def is_duplicate_comment(
    existing_discussions: List[Dict[str, Any]], path: str, line: int, comment: str
) -> bool:
    """Проверить, существует ли уже комментарий на этой позиции"""
    # Нормализуем комментарий для сравнения (убираем лишние пробелы)
    normalized_comment = " ".join(comment.split())
    
    for disc in existing_discussions:
        notes = disc.get("notes") or []
        for note in notes:
            # Проверяем, что это AI комментарий
            body = note.get("body") or ""
            if not body.startswith("🤖"):
                continue
            
            # Проверяем позицию
            position = note.get("position")
            if not position:
                continue
                
            note_path = position.get("new_path") or position.get("old_path")
            note_line = position.get("new_line") or position.get("old_line")
            
            # Проверяем совпадение файла и строки
            if note_path != path or note_line != line:
                continue
            
            # Нормализуем body для сравнения
            normalized_body = " ".join(body.split())
            
            # Проверяем, что текст комментария совпадает (не просто содержится)
            if normalized_comment in normalized_body:
                return True
    return False


async def post_inline_discussion(
    client: httpx.AsyncClient,
    project_id: int,
    mr_iid: int,
    diff_refs: Dict[str, str],
    item: ReviewItem,
    existing_discussions: List[Dict[str, Any]],
) -> bool:
    base_sha = diff_refs.get("base_sha")
    start_sha = diff_refs.get("start_sha")
    head_sha = diff_refs.get("head_sha")
    if not (base_sha and start_sha and head_sha):
        return False

    # Проверяем на дубликаты
    if is_duplicate_comment(existing_discussions, item.path, item.line, item.comment):
        return True  # Считаем успешным, т.к. комментарий уже есть

    body = {
        "body": f"🤖 **AI review ({item.severity})**: {item.comment}",
        "position": {
            "position_type": "text",
            "base_sha": base_sha,
            "start_sha": start_sha,
            "head_sha": head_sha,
            "new_path": item.path,
            "new_line": item.line,
        },
    }
    try:
        await gitlab_post(
            client, f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/discussions", body
        )
        return True
    except Exception:
        return False


async def post_general_note(client: httpx.AsyncClient, project_id: int, mr_iid: int, text: str) -> None:
    await gitlab_post(client, f"/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes", {"body": text})


def cleanup_old_cache_entries() -> None:
    """Очистить старые записи из кэша"""
    current_time = time.time()
    keys_to_remove = [k for k, v in PROCESSED_COMMITS.items() if current_time - v > CACHE_TTL]
    for k in keys_to_remove:
        PROCESSED_COMMITS.pop(k, None)


def is_commit_processed(project_id: int, mr_iid: int, sha: str) -> bool:
    """Проверить, был ли уже обработан этот коммит"""
    cleanup_old_cache_entries()
    key = f"{project_id}:{mr_iid}:{sha}"
    return key in PROCESSED_COMMITS


def mark_commit_processed(project_id: int, mr_iid: int, sha: str) -> None:
    """Отметить коммит как обработанный"""
    key = f"{project_id}:{mr_iid}:{sha}"
    PROCESSED_COMMITS[key] = time.time()


async def process_merge_request(payload: Dict[str, Any]) -> None:
    async with SEM:
        try:
            project_id = extract_project_id(payload)
            mr_iid = extract_mr_iid(payload)
        except Exception as e:
            return

        async with httpx.AsyncClient() as client:
            # Получаем diff_refs для проверки SHA
            diff_refs, files = await get_mr_diff_refs_and_changes(client, project_id, mr_iid)
            head_sha = diff_refs.get("head_sha", "")
            
            # Проверяем, не обрабатывали ли мы уже этот коммит
            if head_sha and is_commit_processed(project_id, mr_iid, head_sha):
                return  # Уже обрабатывали этот коммит
            
            # Получаем существующие discussions для проверки дубликатов
            existing_discussions = await get_existing_discussions(client, project_id, mr_iid)
            
            # Пытаемся получить diff только последнего коммита
            # Проверяем, есть ли информация о последнем коммите в payload
            last_commit = None
            obj_attrs = payload.get("object_attributes") or {}
            if obj_attrs.get("last_commit"):
                last_commit = obj_attrs["last_commit"].get("id")
            
            # Если это update и есть last_commit, используем только его diff
            action = obj_attrs.get("action")
            if action == "update" and last_commit:
                try:
                    files = await get_commit_diff(client, project_id, last_commit)
                except Exception:
                    # Если не получилось - используем весь MR diff
                    pass
            
            diff_text = build_diff_text(files)

            if not diff_text.strip():
                await post_general_note(client, project_id, mr_iid, "🤖 AI review: изменений для анализа нет.")
                if head_sha:
                    mark_commit_processed(project_id, mr_iid, head_sha)
                return

            try:
                items = await call_opus_anthropic(client, diff_text)
            except Exception as e:
                await post_general_note(client, project_id, mr_iid, f"🤖 AI review: ошибка вызова модели: `{e}`")
                if head_sha:
                    mark_commit_processed(project_id, mr_iid, head_sha)
                return

            diff_map = {}
            for f in files:
                p = f.get("new_path") or f.get("old_path") or ""
                d = f.get("diff") or ""
                if p:
                    diff_map[p] = d

            posted = 0
            fallback: List[str] = []

            for it in items[:20]:
                d = diff_map.get(it.path, "")
                if not d or not line_in_diff(d, it.line):
                    fallback.append(f"- `{it.path}:{it.line}` ({it.severity}) {it.comment}")
                    continue

                ok = await post_inline_discussion(
                    client, project_id, mr_iid, diff_refs, it, existing_discussions
                )
                if ok:
                    posted += 1
                else:
                    fallback.append(f"- `{it.path}:{it.line}` ({it.severity}) {it.comment}")

            if fallback:
                text = "🤖 **AI review (не удалось привязать inline):**\n" + "\n".join(fallback)
                await post_general_note(client, project_id, mr_iid, text)

            if posted == 0 and not fallback:
                await post_general_note(client, project_id, mr_iid, "🤖 AI review: критичных замечаний не найдено.")
            
            # Отмечаем коммит как обработанный
            if head_sha:
                mark_commit_processed(project_id, mr_iid, head_sha)


@app.post("/ai-review")
async def ai_review_webhook(
    request: Request,
    x_gitlab_token: Optional[str] = Header(default=None, alias="X-Gitlab-Token"),
    x_gitlab_event: Optional[str] = Header(default=None, alias="X-Gitlab-Event"),
):
    if x_gitlab_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook token")

    payload = await request.json()

    # Merge request hook
    event = x_gitlab_event or ""
    if "Merge Request Hook" not in event and payload.get("object_kind") != "merge_request":
        return {"ok": True, "ignored": True}

    attrs = payload.get("object_attributes") or {}
    action = attrs.get("action")
    if action not in ("open", "update", "reopen"):
        return {"ok": True, "ignored_action": action}

    asyncio.create_task(process_merge_request(payload))
    return {"ok": True}
