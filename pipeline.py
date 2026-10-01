"""
Обработка записей: облако Zoom → аудио → Transkriptor → отчёт по форме → Google Диск → ссылка в группу.
Запускается фоном из бота каждые RECORDINGS_CHECK_MIN минут (и вручную командой /process).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import shutil
from datetime import datetime, timedelta

import gdrive
import registry
import report_writer
import storage
import transcriber
import zoom_client
from config import (
    ALLOWED_CHAT_IDS,
    DATA_DIR,
    EXPERTS,
    ORGANIZATIONS,
    MIN_RECORDING_MIN,
    PROCESS_SINCE,
    ZOOM_BACKUP_TRASH,
    RECORDINGS_CHECK_MIN,
    RECORDINGS_LOOKBACK_DAYS,
    ZOOM_TRASH_AFTER_DAYS,
)
from timeutil import MSK, fmt_date

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BLOCK_RE = re.compile(r"Модуль\s*(\d+)\D+?встреча\s*(\d+)", re.I)
_lock = asyncio.Lock()


def match_org(topic: str):
    topic = (topic or "").strip()
    for org in sorted(ORGANIZATIONS, key=len, reverse=True):
        if topic.startswith(org):
            return org
    return None


def _start_msk(m: dict) -> datetime:
    return datetime.fromisoformat(m["start_time"].replace("Z", "+00:00")).astimezone(MSK).replace(tzinfo=None)


VIDEO_PRIORITY = ["shared_screen_with_speaker_view", "shared_screen_with_speaker_view(CC)", "speaker_view",
                  "active_speaker", "shared_screen_with_gallery_view", "gallery_view", "shared_screen"]


def _pick_video(files: list):
    video = [f for f in files if f.get("file_type") == "MP4"]
    rank = lambda f: VIDEO_PRIORITY.index(f.get("recording_type")) if f.get("recording_type") in VIDEO_PRIORITY else 99
    return min(video, key=rank) if video else None


def _pick_audio(files: list):
    audio = [f for f in files if f.get("file_type") == "M4A"]
    if audio:
        return audio[0]
    video = [f for f in files if f.get("file_type") == "MP4"]
    return min(video, key=lambda f: f.get("file_size", 0)) if video else None


async def sync_registry(func, *args) -> None:
    """Реестр — вспомогательный: его сбой не должен ломать встречи и отчёты."""
    try:
        await asyncio.to_thread(func, *args)
    except Exception:
        logger.exception("Не удалось обновить реестр встреч")


async def run_loop(app) -> None:
    await asyncio.sleep(20)
    await sync_registry(registry.sync_all)
    while True:
        try:
            await check_recordings(app.bot)
        except Exception:
            logger.exception("Проверка записей упала")
        try:
            await check_uploads(app.bot)  # свои записи экспертов, загруженные на Диск
        except Exception:
            logger.exception("Обработка своих записей упала")
        await sync_registry(registry.sync_changed)  # «прошла», «записи нет» — по времени
        await asyncio.sleep(RECORDINGS_CHECK_MIN * 60)


async def check_recordings(bot) -> dict:
    """Обрабатывает новые записи. Возвращает счётчики для /process."""
    stats = {"done": 0, "waiting": 0, "failed": 0}
    if _lock.locked():
        return {"busy": True}
    async with _lock:
        processed = storage.load("processed")
        recordings = []
        for acc in zoom_client.ACCOUNTS:  # основной и, если настроен, резервный аккаунт
            try:
                recordings += [(acc, m) for m in await zoom_client.list_recordings(RECORDINGS_LOOKBACK_DAYS, acc)]
            except Exception:
                logger.exception("Не удалось получить записи (аккаунт %s)", acc)
        for acc, m in recordings:
            uuid = m["uuid"]
            state = processed.get(uuid, {})
            if state.get("status") == "done":
                await _after_report(bot, m, state, acc)
                continue
            if state.get("status") in ("skipped", "failed"):
                continue
            org = match_org(m.get("topic", ""))
            general = storage.load("meetings").get(str(m.get("id")), {}).get("kind") == "general"
            too_old = PROCESS_SINCE and _start_msk(m).date().isoformat() < PROCESS_SINCE
            if not (org or general) or too_old:
                storage.update("processed", uuid, {"status": "skipped", "topic": m.get("topic", ""), "account": acc})
                continue
            files = m.get("recording_files", [])
            if not files or any(f.get("status") != "completed" for f in files) or not _pick_audio(files):
                stats["waiting"] += 1  # Zoom ещё готовит запись
                continue
            if int(m.get("duration") or 0) < MIN_RECORDING_MIN:
                # Участник зашёл по ссылке заранее и вышел — автозапись дала пустышку, это не встреча
                logger.info("Пропускаю короткую запись «%s» (%s мин)", m.get("topic"), m.get("duration"))
                storage.update("processed", uuid, {"status": "skipped", "topic": m.get("topic", ""), "account": acc,
                                                   "note": f"короткая запись ({m.get('duration')} мин) — случайный заход"})
                continue
            try:
                await (process_general(bot, m, state, acc) if general else process_one(bot, m, org, state, acc))
                stats["done"] += 1
                await _after_report(bot, m, storage.load("processed").get(uuid, {}), acc)
            except Exception as e:
                logger.exception("Не удалось обработать запись %s", m.get("topic"))
                attempts = state.get("attempts", 0) + 1
                storage.update("processed", uuid, {"attempts": attempts, "error": str(e)[:500]})
                if attempts >= MAX_ATTEMPTS:
                    storage.update("processed", uuid, {"status": "failed"})
                    stats["failed"] += 1
                    await _notify(bot, m, f"⚠️ Не получилось обработать запись встречи "
                                          f"«{html.escape(m.get('topic', ''))}»: {html.escape(str(e)[:300])}")
    return stats


async def process_one(bot, m: dict, org: str, state: dict, acc: str = zoom_client.MAIN) -> None:
    uuid, topic = m["uuid"], m.get("topic", "")
    info = storage.load("meetings").get(str(m.get("id")), {})
    start = _start_msk(m)
    block = info.get("block")
    if not block:
        found = BLOCK_RE.search(topic)
        block = f"Модуль {found[1]}, встреча {found[2]}" if found else "модуль и номер встречи не указаны"
    meta = {"org": org, "expert": EXPERTS.get(org, ""), "block": block,
            "date": fmt_date(start.date()), "date_short": f"{start:%d.%m.%Y}", "topic": topic}
    previous = ""
    prev = storage.load("meetings").get(info.get("prev_meeting_id") or "", {})
    if info.get("part") and prev.get("report_link"):
        # Продолжение: даём нейросети отчёт прошлой встречи, чтобы она видела, что уже было сделано
        meta["prev_date"] = fmt_date(datetime.fromisoformat(prev["start"]).date())
        try:
            previous = await asyncio.to_thread(gdrive.export_text, prev["report_link"])
        except Exception:
            logger.exception("Не удалось прочитать прошлый отчёт %s", prev.get("report_link"))
    stamp = start.strftime("%Y-%m-%d")

    workdir = os.path.join(DATA_DIR, "tmp", re.sub(r"[^\w-]", "_", uuid))
    os.makedirs(workdir, exist_ok=True)
    try:
        files = m["recording_files"]
        audio = _pick_audio(files)
        audio_path = os.path.join(workdir, "audio." + audio.get("file_extension", "m4a").lower())
        logger.info("Скачиваю запись «%s»", topic)
        await zoom_client.download(audio["download_url"], audio_path, acc)

        chat_text = ""
        chat = next((f for f in files if f.get("file_type") == "CHAT"), None)
        if chat:
            chat_path = os.path.join(workdir, "chat.txt")
            await zoom_client.download(chat["download_url"], chat_path, acc)
            with open(chat_path, encoding="utf-8", errors="ignore") as f:
                chat_text = f.read()

        segments = await asyncio.to_thread(transcriber.transcribe, audio_path)
        transcript = transcriber.as_text(segments)
        data = await asyncio.to_thread(report_writer.draft_report, meta, transcript, chat_text, previous)
        report_path = report_writer.fill_template(meta, data, os.path.join(workdir, "report.docx"))

        transcript_path = os.path.join(workdir, "transcript.txt")
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(f"Расшифровка встречи: {topic}\n{meta['date']}, {start:%H:%M} по Москве\n"
                    f"Эксперт: {meta['expert']}\n\n{transcript}\n")

        folder = await asyncio.to_thread(gdrive.folder_for, org)
        # Ссылки сохраняем по мере загрузки, чтобы при повторе не плодить дубли
        report_link = state.get("report_link") or await asyncio.to_thread(
            gdrive.upload_as_doc, report_path, f"{stamp} Отчёт — {topic}", folder, gdrive.DOCX)
        storage.update("processed", uuid, {"report_link": report_link})
        transcript_link = state.get("transcript_link") or await asyncio.to_thread(
            gdrive.upload_as_doc, transcript_path, f"{stamp} Расшифровка — {topic}", folder, "text/plain")
        storage.update("processed", uuid, {"transcript_link": transcript_link, "status": "done",
                                           "topic": topic, "meeting_id": str(m.get("id")),
                                           "processed_at": datetime.now().isoformat()})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    await _to_registry(m, org, block, start, report_link, transcript_link, acc)

    mention = info.get("mention", "")
    await _notify(bot, m, "\n".join([
        "📄 <b>Отчёт по встрече готов</b>",
        "",
        f"📌 {html.escape(topic)}",
        f"📅 {meta['date']}, {start:%H:%M} по Москве",
        f"👤 Эксперт: {html.escape(meta['expert'])}",
        "",
        f"📝 Отчёт: {report_link}",
        f"🗒 Расшифровка: {transcript_link}",
        "",
        (f"{mention}, " if mention else "") + "это черновик по записи — проверьте и дополните своим суждением.",
    ]))


async def _after_report(bot, m: dict, state: dict, acc: str = zoom_client.MAIN) -> None:
    """Шаги после отчёта: видео на Диск, затем (через ZOOM_TRASH_AFTER_DAYS) запись — в корзину Zoom."""
    if not state.get("video_link") and state.get("video_attempts", 0) < MAX_ATTEMPTS and _pick_video(m["recording_files"]):
        await _video_step(bot, m, state, acc)
        state = storage.load("processed").get(m["uuid"], {})
    if acc == zoom_client.BACKUP and not ZOOM_BACKUP_TRASH:
        return  # на резервном аккаунте записи не трогаем, пока это не разрешено настройкой
    if (ZOOM_TRASH_AFTER_DAYS and state.get("video_verified") and not state.get("zoom_trashed")
            and datetime.now() - datetime.fromisoformat(state["video_at"]) >= timedelta(days=ZOOM_TRASH_AFTER_DAYS)):
        try:
            await zoom_client.trash_recordings(m["uuid"], acc)
        except Exception:
            logger.exception("Не удалось отправить запись в корзину Zoom: %s", m.get("topic"))
            return
        logger.info("Запись «%s» отправлена в корзину Zoom", m.get("topic"))
        storage.update("processed", m["uuid"], {"zoom_trashed": datetime.now().isoformat()})
        mid = str(m.get("id"))
        storage.update("meetings", mid, {"zoom_trashed": True})
        await sync_registry(registry.sync, [mid])


async def _video_step(bot, m: dict, state: dict, acc: str = zoom_client.MAIN) -> None:
    uuid, topic = m["uuid"], m.get("topic", "")
    video = _pick_video(m["recording_files"])
    stamp = _start_msk(m).strftime("%Y-%m-%d")
    workdir = os.path.join(DATA_DIR, "tmp", "video_" + re.sub(r"[^\w-]", "_", uuid))
    os.makedirs(workdir, exist_ok=True)
    path = os.path.join(workdir, "video.mp4")
    try:
        logger.info("Скачиваю видео «%s» (%.0f МБ)", topic, video.get("file_size", 0) / 1024 ** 2)
        await zoom_client.download(video["download_url"], path, acc)
        folder = await asyncio.to_thread(gdrive.subfolder, await asyncio.to_thread(_base_folder, m), "Видео")
        link, size = await asyncio.to_thread(gdrive.upload_video, path, f"{stamp} Видео — {topic}.mp4", folder)
    except Exception as e:
        logger.exception("Не удалось перенести видео «%s»", topic)
        attempts = state.get("video_attempts", 0) + 1
        storage.update("processed", uuid, {"video_attempts": attempts, "video_error": str(e)[:500]})
        if attempts >= MAX_ATTEMPTS:
            await _notify(bot, m, f"⚠️ Не получилось перенести видео встречи «{html.escape(topic)}» на Диск: "
                                  f"{html.escape(str(e)[:300])}. Запись остаётся в облаке Zoom.")
        return
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    # Удалять из Zoom разрешаем только если на Диске ровно тот же размер, что и в Zoom
    verified = bool(size) and size == int(video.get("file_size") or 0)
    storage.update("processed", uuid, {"video_link": link, "video_verified": verified, "account": acc,
                                       "video_at": datetime.now().isoformat()})
    mid = str(m.get("id"))
    storage.update("meetings", mid, {"video_link": link})
    await sync_registry(registry.sync, [mid])
    await _notify(bot, m, f"🎬 Видео встречи «{html.escape(topic)}» на Диске: {link}")


def _base_folder(m: dict) -> str:
    """Папка встречи на Диске: папка организации или — для общих встреч программы — своя папка."""
    org = match_org(m.get("topic", ""))
    if org:
        return gdrive.folder_for(org)
    info = storage.load("meetings").get(str(m.get("id")), {})
    return gdrive.subfolder(gdrive.GOOGLE_DRIVE_ROOT_ID, info.get("folder") or "Мастер-майнды")


async def process_general(bot, m: dict, state: dict, acc: str = zoom_client.MAIN) -> None:
    """Общая встреча программы (мастер-майнд): только расшифровка, без отчёта эксперта. Видео — дальше, как у всех."""
    uuid, topic = m["uuid"], m.get("topic", "")
    start = _start_msk(m)
    workdir = os.path.join(DATA_DIR, "tmp", re.sub(r"[^\w-]", "_", uuid))
    os.makedirs(workdir, exist_ok=True)
    try:
        audio = _pick_audio(m["recording_files"])
        audio_path = os.path.join(workdir, "audio." + audio.get("file_extension", "m4a").lower())
        logger.info("Скачиваю запись «%s»", topic)
        await zoom_client.download(audio["download_url"], audio_path, acc)
        transcript = transcriber.as_text(await asyncio.to_thread(transcriber.transcribe, audio_path))
        path = os.path.join(workdir, "transcript.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"Расшифровка встречи: {topic}\n{fmt_date(start.date())}, {start:%H:%M} по Москве\n\n{transcript}\n")
        folder = await asyncio.to_thread(_base_folder, m)
        link = state.get("transcript_link") or await asyncio.to_thread(
            gdrive.upload_as_doc, path, f"{start:%Y-%m-%d} Расшифровка — {topic}", folder, "text/plain")
        storage.update("processed", uuid, {"transcript_link": link, "status": "done", "topic": topic,
                                           "meeting_id": str(m.get("id")), "processed_at": datetime.now().isoformat()})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    await _to_registry(m, "", "", start, "", link, acc)
    await _notify(bot, m, f"🗒 Расшифровка встречи «{html.escape(topic)}» готова: {link}\nВидео пришлю отдельно.")


async def _to_registry(m: dict, org: str, block: str, start: datetime, report_link: str, transcript_link: str,
                       acc: str = zoom_client.MAIN) -> None:
    mid = str(m.get("id"))
    rec = {"recording_url": m.get("share_url", ""), "recording_code": ""}
    try:
        details = await zoom_client.recording_details(m["uuid"], acc)
        rec = {"recording_url": details.get("share_url") or rec["recording_url"],
               "recording_code": details.get("password", "")}
    except Exception:
        logger.exception("Не удалось получить ссылку и код записи %s", mid)
    base = {} if mid in storage.load("meetings") else {  # встреча создана не через бота
        "org": org, "block": block, "topic": m.get("topic", ""), "start": start.isoformat(),
        "duration": m.get("duration") or 60}
    storage.update("meetings", mid, {**base, **rec, "account": acc,
                                     "report_link": report_link, "transcript_link": transcript_link})
    await sync_registry(registry.sync, [mid])


# ---------- свои записи экспертов (загруженные на Диск, не из Zoom) ----------

_uploads_lock = asyncio.Lock()


async def check_uploads(bot) -> int:
    """Обрабатывает загрузки, по которым эксперт нажал «Я загрузил(а)»."""
    if _uploads_lock.locked():
        return 0
    done = 0
    async with _uploads_lock:
        for job_id, job in storage.load("uploads").items():
            if job.get("status") != "ready":
                continue
            try:
                await process_upload(bot, job_id, job)
                done += 1
            except Exception as e:
                logger.exception("Не удалось обработать свою запись %s", job_id)
                attempts = job.get("attempts", 0) + 1
                storage.update("uploads", job_id, {"attempts": attempts, "error": str(e)[:500],
                                                   "status": "failed" if attempts >= MAX_ATTEMPTS else "ready"})
                if attempts >= MAX_ATTEMPTS:
                    await _send(bot, job.get("chat_id"),
                                f"⚠️ Не получилось обработать записи встречи «{html.escape(job.get('topic', ''))}»: "
                                f"{html.escape(str(e)[:300])}\nФайлы остались в папке на Диске.")
    return done


async def process_upload(bot, job_id: str, job: dict) -> None:
    storage.update("uploads", job_id, {"status": "processing"})
    start = datetime.fromisoformat(job["start"])
    org, topic = job["org"], job["topic"]
    stamp = start.strftime("%Y-%m-%d")
    meta = {"org": org, "expert": EXPERTS.get(org, ""), "block": job["block"],
            "date": fmt_date(start.date()), "date_short": f"{start:%d.%m.%Y}", "topic": topic}
    previous = ""
    prev = storage.load("meetings").get(job.get("prev_meeting_id") or "", {})
    if job.get("part") and prev.get("report_link"):
        meta["prev_date"] = fmt_date(datetime.fromisoformat(prev["start"]).date())
        try:
            previous = await asyncio.to_thread(gdrive.export_text, prev["report_link"])
        except Exception:
            logger.exception("Не удалось прочитать прошлый отчёт %s", prev.get("report_link"))

    files = await asyncio.to_thread(gdrive.list_media, job["folder_id"])
    if not files:
        storage.update("uploads", job_id, {"status": "waiting"})
        await _send(bot, job.get("chat_id"), f"В папке встречи «{html.escape(topic)}» файлов не нашлось — "
                                             "загрузите записи и нажмите «Я загрузил(а)» ещё раз.")
        return

    workdir = os.path.join(DATA_DIR, "tmp", job_id)
    os.makedirs(workdir, exist_ok=True)
    try:
        parts = []
        for n, f in enumerate(files, 1):
            path = os.path.join(workdir, f"part{n}_" + re.sub(r"[^\w.-]", "_", f["name"])[-60:])
            logger.info("Скачиваю с Диска «%s» (%s)", f["name"], topic)
            await asyncio.to_thread(gdrive.download_file, f["id"], path)
            text = transcriber.as_text(await asyncio.to_thread(transcriber.transcribe, path))
            parts.append(f"=== Часть {n} из {len(files)}: {f['name']} ===\n{text}" if len(files) > 1 else text)
            os.remove(path)
        transcript = "\n\n".join(parts)

        data = await asyncio.to_thread(report_writer.draft_report, meta, transcript, "", previous)
        report_path = report_writer.fill_template(meta, data, os.path.join(workdir, "report.docx"))
        transcript_path = os.path.join(workdir, "transcript.txt")
        with open(transcript_path, "w", encoding="utf-8") as fh:
            fh.write(f"Расшифровка встречи: {topic}\n{meta['date']}, {start:%H:%M} по Москве\n"
                     f"Эксперт: {meta['expert']}\nЗаписей: {len(files)}\n\n{transcript}\n")

        folder = await asyncio.to_thread(gdrive.folder_for, org)
        report_link = job.get("report_link") or await asyncio.to_thread(
            gdrive.upload_as_doc, report_path, f"{stamp} Отчёт — {topic}", folder, gdrive.DOCX)
        storage.update("uploads", job_id, {"report_link": report_link})
        transcript_link = job.get("transcript_link") or await asyncio.to_thread(
            gdrive.upload_as_doc, transcript_path, f"{stamp} Расшифровка — {topic}", folder, "text/plain")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # Сами записи переносим в «Видео» организации, а временную папку убираем
    video_folder = await asyncio.to_thread(gdrive.subfolder, folder, "Видео")
    video_link = ""
    for n, f in enumerate(files, 1):
        ext = os.path.splitext(f["name"])[1] or ".mp4"
        suffix = f" (часть {n})" if len(files) > 1 else ""
        try:
            link = await asyncio.to_thread(gdrive.move_file, f["id"], video_folder,
                                           f"{stamp} Запись — {topic}{suffix}{ext}")
            video_link = video_link or link
        except Exception:
            logger.exception("Не удалось перенести файл %s", f.get("name"))
    try:
        await asyncio.to_thread(gdrive.trash, job["folder_id"])
    except Exception:
        logger.exception("Не удалось убрать папку загрузки %s", job_id)

    storage.update("meetings", job_id, {
        "meeting_id": job_id, "account": "own", "status": "active", "org": org, "topic": topic,
        "block": job["block"], "module": job.get("module"), "number": job.get("number"),
        "part": job.get("part"), "prev_meeting_id": job.get("prev_meeting_id", ""),
        "topic_extra": job.get("topic_extra", ""), "start": job["start"], "duration": 60,
        "chat_id": job.get("chat_id"), "owner_id": job.get("owner_id"), "mention": job.get("mention", ""),
        "report_link": report_link, "transcript_link": transcript_link, "video_link": video_link,
    })
    storage.update("uploads", job_id, {"status": "done", "transcript_link": transcript_link,
                                       "processed_at": datetime.now().isoformat()})
    await sync_registry(registry.sync, [job_id])
    await _send(bot, job.get("chat_id"), "\n".join([
        "📄 <b>Отчёт по вашей записи готов</b>", "",
        f"📌 {html.escape(topic)}",
        f"📅 {meta['date']}, {start:%H:%M} по Москве",
        f"👤 Эксперт: {html.escape(meta['expert'])}",
        f"🎧 Записей обработано: {len(files)}", "",
        f"📝 Отчёт: {report_link}",
        f"🗒 Расшифровка: {transcript_link}",
    ] + ([f"🎬 Запись на Диске: {video_link}"] if video_link else []) + [
        "", (f"{job.get('mention')}, " if job.get("mention") else "")
        + "это черновик по записи — проверьте и дополните своим суждением.",
    ]))


async def _send(bot, chat_id, text: str) -> None:
    if not chat_id:
        return
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("Не удалось отправить сообщение в чат %s", chat_id)


async def _notify(bot, m: dict, text: str) -> None:
    info = storage.load("meetings").get(str(m.get("id")), {})
    chat_id = info.get("chat_id") or next(iter(ALLOWED_CHAT_IDS), None)
    if not chat_id:
        return
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("Не удалось отправить сообщение в чат %s", chat_id)
