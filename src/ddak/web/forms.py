"""python-multipart 없이 작은 URL-encoded 관리 폼을 읽는다."""

from urllib.parse import parse_qs

from fastapi import HTTPException, Request


async def parse_form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="지원하지 않는 폼 형식")
    body = await request.body()
    # 템플릿 맨 앞의 공개 위치 정보만 보존한다. 크기/형식 실패에도 같은 폼으로
    # 돌아가며, 이 정보는 승인이나 입력 검증에 쓰지 않는다.
    metadata = {}
    for field in body[:4096].split(b"&")[:-1]:
        if field.partition(b"=")[0] not in {b"_form_id", b"_return_to"}:
            continue
        try:
            parsed_location = parse_qs(field.decode("utf-8"), strict_parsing=True)
        except (UnicodeDecodeError, ValueError):
            continue
        metadata.update({key: values[-1] for key, values in parsed_location.items()})
    request.state.form_metadata = metadata
    if len(body) > 64 * 1024:
        raise HTTPException(status_code=413, detail="폼이 너무 크다")
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="폼 형식 오류") from exc
    form = {key: values[-1] for key, values in parsed.items() if values}
    request.state.form_metadata = {
        key: form.pop(key) for key in ("_form_id", "_return_to") if key in form
    }
    request.state.submitted_form = form
    return form
