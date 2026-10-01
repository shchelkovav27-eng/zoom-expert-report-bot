"""
Google Диск: поиск подпапки организации и загрузка файлов как Google-документов.
Вход — от имени владельца Диска (google_token.json с refresh_token).
"""

from __future__ import annotations

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from config import GOOGLE_DRIVE_ROOT_ID, GOOGLE_TOKEN_FILE

FOLDER = "application/vnd.google-apps.folder"
GDOC = "application/vnd.google-apps.document"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def credentials() -> Credentials:
    creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE)
    if not creds.valid:
        creds.refresh(Request())
    return creds


def service():
    return build("drive", "v3", credentials=credentials(), cache_discovery=False)


def subfolder(parent_id: str, name: str) -> str:
    """ID папки `name` внутри `parent_id`; если её нет — создаёт."""
    svc = service()
    q_name = name.replace("\\", "\\\\").replace("'", "\\'")
    found = svc.files().list(
        q=f"'{parent_id}' in parents and name = '{q_name}' and mimeType = '{FOLDER}' and trashed = false",
        fields="files(id)",
    ).execute().get("files", [])
    if found:
        return found[0]["id"]
    return svc.files().create(body={"name": name, "mimeType": FOLDER, "parents": [parent_id]},
                              fields="id").execute()["id"]


def folder_for(org: str) -> str:
    """ID подпапки организации в общей папке."""
    return subfolder(GOOGLE_DRIVE_ROOT_ID, org)


def upload_video(path: str, name: str, folder_id: str) -> tuple:
    """Загружает видео частями (переживает сбои связи). Возвращает (ссылка, размер в байтах на Диске)."""
    media = MediaFileUpload(path, mimetype="video/mp4", resumable=True, chunksize=16 * 1024 * 1024)
    request = service().files().create(body={"name": name, "parents": [folder_id]}, media_body=media,
                                       fields="id, webViewLink, size")
    response = None
    while response is None:
        _, response = request.next_chunk(num_retries=5)
    return response["webViewLink"], int(response.get("size", 0))


def list_media(folder_id: str) -> list:
    """Аудио- и видеофайлы в папке, от старых к новым: [{id, name, mimeType, size}]."""
    files = service().files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(id, name, mimeType, size, createdTime)", orderBy="createdTime",
    ).execute().get("files", [])
    return [f for f in files if str(f.get("mimeType", "")).startswith(("audio/", "video/"))]


def download_file(file_id: str, dest: str) -> None:
    """Скачивает файл с Диска на сервер частями."""
    from googleapiclient.http import MediaIoBaseDownload
    request = service().files().get_media(fileId=file_id)
    with open(dest, "wb") as f:
        downloader = MediaIoBaseDownload(f, request, chunksize=16 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=5)


def move_file(file_id: str, new_parent: str, name: str = None) -> str:
    """Переносит файл в другую папку (и при желании переименовывает). Возвращает ссылку."""
    svc = service()
    old = ",".join(svc.files().get(fileId=file_id, fields="parents").execute().get("parents", []))
    body = {"name": name} if name else {}
    f = svc.files().update(fileId=file_id, addParents=new_parent, removeParents=old,
                           body=body, fields="id, webViewLink").execute()
    return f["webViewLink"]


def trash(file_id: str) -> None:
    """Отправляет файл или папку в корзину Диска (восстановимо)."""
    service().files().update(fileId=file_id, body={"trashed": True}).execute()


def export_text(link_or_id: str, limit: int = 40000) -> str:
    """Текст Google-документа (например, прошлого отчёта) по ссылке или ID."""
    fid = link_or_id.split("/d/")[1].split("/")[0] if "/d/" in link_or_id else link_or_id
    data = service().files().export(fileId=fid, mimeType="text/plain").execute()
    return (data.decode("utf-8") if isinstance(data, bytes) else str(data))[:limit]


def upload_as_doc(path: str, name: str, folder_id: str, mimetype: str) -> str:
    """Загружает .docx/.txt и конвертирует в Google-документ. Возвращает ссылку."""
    f = service().files().create(
        body={"name": name, "parents": [folder_id], "mimeType": GDOC},
        media_body=MediaFileUpload(path, mimetype=mimetype, resumable=False),
        fields="id, webViewLink",
    ).execute()
    return f["webViewLink"]
