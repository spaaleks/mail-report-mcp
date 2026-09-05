from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from .mail.config import ConfigError, load_config
from .runtime import mcp
from .storage import attachments as att
from .storage import bundles, tickets, uploads


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    try:
        load_config()
        smtp_configured = True
    except ConfigError:
        smtp_configured = False
    return JSONResponse({"status": "ok", "smtp_configured": smtp_configured})


DRAIN_BUDGET_MULTIPLE = 4


async def _drain(request: Request, limit: int) -> None:
    budget = limit * DRAIN_BUDGET_MULTIPLE
    seen = 0
    try:
        async for chunk in request.stream():
            seen += len(chunk)
            if seen > budget:
                return
    except Exception:
        return


async def upload_attachment(request: Request) -> JSONResponse:
    limit = att.max_file_bytes()
    ticket = request.headers.get("x-upload-ticket")
    budget = att.max_total_bytes()
    if ticket:
        try:
            budget = min(budget, tickets.check(ticket)["bytes_left"])
        except tickets.TicketError as e:
            await _drain(request, limit)
            return JSONResponse({"error": str(e)}, status_code=403)

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > budget:
        await _drain(request, budget)
        return JSONResponse({"error": f"Body exceeds the {budget}-byte budget."}, status_code=413)

    content_type = (request.headers.get("content-type") or "").lower()
    files: list[tuple[str, bytes]] = []

    if content_type.startswith("multipart/form-data"):
        form = await request.form(max_files=1000, max_part_size=limit)
        try:
            for _, value in form.multi_items():
                if not hasattr(value, "read"):
                    continue
                files.append((value.filename or "attachment", await value.read()))
        finally:
            await form.close()
        if not files:
            return JSONResponse({"error": "No file part in the form."}, status_code=400)
        unpack = request.headers.get("x-unpack")
        if len(files) == 1 and uploads.wants_unpack(files[0][0], unpack, files[0][1]):
            try:
                files = uploads.unpack_zip(files[0][1], budget)
            except uploads.UploadError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
    else:
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > budget:
                await _drain(request, budget)
                return JSONResponse({"error": f"Body exceeds the {budget}-byte budget."}, status_code=413)
        data = bytes(chunks)
        name = request.headers.get("x-filename") or request.query_params.get("filename") or "attachment"
        if uploads.wants_unpack(name, request.headers.get("x-unpack"), data):
            try:
                files = uploads.unpack_zip(data, budget)
            except uploads.UploadError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        else:
            files = [(name, data)]

    oversized = [(n, len(d)) for n, d in files if len(d) > limit]
    if oversized:
        name, size = oversized[0]
        return JSONResponse(
            {"error": f"{name!r} is {size} bytes, over the {limit}-byte per-file limit."},
            status_code=413,
        )

    total = sum(len(d) for _, d in files)
    if total > budget:
        return JSONResponse(
            {"error": f"Upload totals {total} bytes, over the {budget}-byte budget."}, status_code=413
        )

    if ticket:
        try:
            tickets.spend(ticket, len(files), total)
        except tickets.TicketError as e:
            return JSONResponse({"error": str(e)}, status_code=403)

    saved: list[dict] = []
    try:
        for name, payload in files:
            saved.append(att.save_upload(name, payload))
    except att.AttachmentError as e:
        att.delete([item["id"] for item in saved])
        return JSONResponse({"error": str(e)}, status_code=400)

    names = [name for name, _ in files]
    report_index = next((i for i, name in enumerate(names) if bundles.is_report(name)), None)
    if report_index is not None:
        entries = [
            (names[i].rsplit("/", 1)[-1], saved[i]["id"])
            for i in range(len(saved))
            if i != report_index
        ]
        bundle = bundles.create(entries, saved[report_index]["id"])
        return JSONResponse(
            {
                **bundle,
                "ids": [item["id"] for item in saved],
                "bytes": total,
                "note": "Pass bundle_id to send_report; report.md becomes the body and its\n"
                "image references are embedded.",
            },
            status_code=201,
        )

    if len(saved) == 1:
        return JSONResponse({**saved[0], "ids": [saved[0]["id"]]}, status_code=201)
    return JSONResponse(
        {"ids": [item["id"] for item in saved], "files": saved, "bytes": total}, status_code=201
    )
