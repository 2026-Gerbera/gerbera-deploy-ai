"""python-multipart 없이 작은 URL-encoded 관리 폼을 읽는다."""

from urllib.parse import parse_qs

from fastapi import HTTPException, Request


async def parse_form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="지원하지 않는 폼 형식")
    body = await request.body()
    if len(body) > 64 * 1024:
        raise HTTPException(status_code=413, detail="폼이 너무 크다")
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="폼 형식 오류") from exc
    return {key: values[-1] for key, values in parsed.items() if values}
