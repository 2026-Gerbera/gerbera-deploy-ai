"""docs/images 그림 생성 스크립트 (SVG). PNG 변환은 _render.sh 참고.
수정 후: python3 _generate.py && bash _render.sh
"""
F = 'font-family="Apple SD Gothic Neo, AppleSDGothicNeo, Noto Sans KR, sans-serif"'
M = 'font-family="Menlo, SF Mono, monospace"'
C = {'purple': ('#EEEDFE', '#534AB7', '#3C3489', '#534AB7'), 'teal': ('#E1F5EE', '#0F6E56', '#085041', '#0F6E56'),
     'gray': ('#F1EFE8', '#5F5E5A', '#2C2C2A', '#5F5E5A'), 'coral': ('#FAECE7', '#993C1D', '#712B13', '#993C1D')}
DEFS = ('<defs><marker id="a" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M2 1L8 5L2 9" fill="none" stroke="#5F5E5A" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></marker></defs>')


def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def txt(x, y, s, size=12, col='#5F5E5A', anchor='start', weight='400', font=F):
    return (f'<text x="{x}" y="{y}" text-anchor="{anchor}" dominant-baseline="central" font-size="{size}" '
            f'font-weight="{weight}" fill="{col}" {font}>{esc(s)}</text>')


def box(x, y, w, h, col, title, sub=None, dash=False):
    f, s, t, st = C[col]
    d = ' stroke-dasharray="5 4"' if dash else ''
    o = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{f}" stroke="{s}" stroke-width="1"{d}/>'
    cx = x + w / 2
    if sub:
        o += txt(cx, y + h / 2 - 9, title, 14, t, 'middle', '600') + txt(cx, y + h / 2 + 11, sub, 12, st, 'middle')
    else:
        o += txt(cx, y + h / 2, title, 14, t, 'middle', '600')
    return o


def arr(x1, y1, x2, y2, dash=False):
    d = ' stroke-dasharray="4 3"' if dash else ''
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#5F5E5A" stroke-width="1.3" marker-end="url(#a)"{d}/>'


def svg(W, H, body):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}"><rect width="{W}" height="{H}" fill="#FFFFFF"/>' + ''.join(body) + '</svg>'


# ---------- 01 흐름 ----------
def img01():
    W, H = 680, 790
    o = [DEFS, txt(340, 24, '파이프라인: 플랜 → 검증 → 빌드 → 배포 → 검증 및 보고', 16, '#2C2C2A', 'middle', '600')]
    o.append(box(230, 48, 220, 56, 'gray', '개발자: "배포해줘"', '로컬 디렉토리 / 깃 주소'))
    o.append(arr(340, 104, 340, 132))
    o.append(box(180, 136, 320, 56, 'purple', '① 플랜 (AI)', '분석 → 계획 JSON (+토글 ON: 이식성 패치)'))
    o.append(arr(340, 192, 340, 220))
    o.append(box(30, 224, 160, 56, 'gray', '코드 수정 토글', 'OFF면 수정 툴 차단'))
    o.append(arr(192, 252, 206, 252))
    o.append(box(210, 224, 260, 56, 'teal', '② 검증 (스크립트)', '계획 검증: 허용 툴·필수 단계·토글'))
    o.append(arr(472, 252, 486, 252))
    o.append(box(490, 224, 160, 56, 'teal', '불합격 시', '재지시 1회 → 규칙 계획'))
    o.append(arr(340, 280, 340, 310))
    o.append('<rect x="30" y="314" width="620" height="104" rx="12" fill="none" stroke="#888780" stroke-width="1" stroke-dasharray="6 4"/>')
    o.append(txt(48, 334, '고정 실행기: AI 없음 (미리 정의한 MCP 툴을 계획 순서대로 코드가 호출)', 13, '#2C2C2A', 'start', '600'))
    o.append(box(50, 350, 280, 56, 'teal', '③ 빌드', 'CodeBuild → ECR (같은 이미지)'))
    o.append(arr(332, 378, 366, 378))
    o.append(box(370, 350, 260, 56, 'teal', '④ 배포 (HTTPS)', '로컬(스테이징) → 클라우드, 클라우드 TLS'))
    o.append(arr(340, 418, 340, 448))
    o.append('<rect x="30" y="452" width="620" height="112" rx="12" fill="none" stroke="#534AB7" stroke-width="1" stroke-dasharray="6 4"/>')
    o.append(txt(48, 472, '⑤ 검증 및 보고', 13, '#3C3489', 'start', '600'))
    o.append(box(50, 490, 280, 56, 'teal', '검증 (규칙)', '헬스·스모크·TLS·교차 비교'))
    o.append(arr(332, 518, 366, 518))
    o.append(txt(338, 506, '통과', 11))
    o.append(box(370, 490, 260, 56, 'purple', '보고 (LLM)', '결과 카드·접속 URL·AI 비용'))
    o.append(arr(190, 546, 190, 602))
    o.append(txt(200, 586, '실패·불일치', 12))
    o.append(box(50, 606, 280, 56, 'teal', '규칙 롤백', '해당 환경만 이전 버전으로'))
    o.append(arr(332, 634, 366, 634))
    o.append(box(370, 606, 260, 56, 'purple', '원인 분석 (AI)', 'Jev 분류 + LLM 설명 → 보고에 포함'))
    o.append(arr(500, 662, 500, 692, True))
    o.append(box(370, 696, 260, 56, 'purple', '코드 수정 (토글 ON일 때만)', '패치 → 승인 → ① 플랜부터 재실행', True))
    for i, (col, label) in enumerate([('purple', 'AI (LLM·Jev)'), ('teal', '스크립트·고정 실행기 (AI 없음)'), ('gray', '사람·설정')]):
        f, s, _, _ = C[col]
        y = 690 + i * 26
        o.append(f'<rect x="50" y="{y}" width="14" height="14" rx="3" fill="{f}" stroke="{s}"/>')
        o.append(txt(72, y + 7, label, 12))
    open('01_pipeline-flow.svg', 'w').write(svg(W, H, o))


# ---------- 02 단계별 툴 ----------
GROUPS = [
    ('① 플랜 (분석·계획·패치)', 'purple', [('receive_deploy_request', '챗봇 요청·코드 업로드', []), ('analyze_project', '환경변수·SQLite·파일 인지', ['AI', '주제']),
                                     ('detect_changed_tiers', '바뀐 tier 판별', []), ('generate_plan', '계획 JSON 생성', ['AI']),
                                     ('patch_db_access', 'SQLite → 공용 SQL', ['AI', '주제']), ('patch_storage', '업로드 → 저장소 어댑터', ['AI', '주제']),
                                     ('patch_config', 'localhost·프록시 헤더 대응', ['AI', '주제'])]),
    ('② 검증 (계획 검증)', 'teal', [('validate_plan', '허용 툴·필수 단계·토글', [])]),
    ('③ 빌드', 'teal', [('build_image', 'CodeBuild → ECR', [])]),
    ('공통', 'gray', [('call_ai', 'AI 호출 관문·비용 기록', ['AI']), ('request_approval', '대화창 승인 버튼', []), ('stream_progress', '로컬·클라우드 진행 화면', [])]),
    ('④ 배포 (로컬 → 클라우드)', 'teal', [('acquire_deploy_lock', '동시 배포 잠금', []), ('ensure_infra', '고정 IaC (데모 중 미실행)', []),
                                    ('inject_env_config', '설정·시크릿 참조 주입', []), ('sync_env_to_cloud', '클라우드 누락 키 동기화', ['주제']),
                                    ('prepare_db', 'DB 컨테이너 / RDS', ['주제']), ('prepare_storage', 'MinIO / S3', ['주제']),
                                    ('ensure_tls', 'ACM 발급·ALB 443·DNS (클라우드)', ['SSL']),
                                    ('deploy_tier', 'tier 순서대로 반영', []), ('rollback_tier', '직전 성공 버전 복귀', [])]),
    ('⑤ 검증 및 보고', 'purple', [('health_check', '헬스·배포 버전 확인', []), ('smoke_test', '쓰기→읽기 핵심 시나리오', []),
                              ('verify_tls', '인증서·리다이렉트·TLS 버전 (클라우드)', ['SSL']),
                              ('compare_env_results', '로컬↔클라우드 결과 비교', ['주제']), ('watch_post_deploy', '배포 후 30~60초 관찰', []),
                              ('collect_diagnostics', '실패 자료 수집·마스킹', []), ('diagnose_parity_gap', '불일치 원인 분류·설명', ['AI', '주제']),
                              ('record_deploy_log', '작업 로그·AI 비용', []), ('post_report', '결과 카드 요약', ['AI'])]),
    ('운영', 'gray', [('preflight_check', '발표 직전 점검', []), ('reset_demo_state', '리허설 상태 초기화', []), ('cleanup', '정리·비용 차단', [])]),
]


def img02():
    W = 1200
    n = sum(len(g[2]) for g in GROUPS)
    o = [txt(30, 34, f'단계별 툴 목록 ({n}개, 확정 주제 기준, 테스트 단계 제외)', 21, '#2C2C2A', 'start', '600'),
         txt(30, 62, 'AI 배지가 없는 툴은 모두 AI 없음(결정적 코드). 툴은 MCP 명세로 미리 정의하고, 배포 실행 때는 실행기(코드)가 계획 순서대로 호출합니다.', 13, '#5F5E5A')]

    def badge(x, cy, label):
        col = C['purple'] if label == 'AI' else (C['teal'] if label == 'SSL' else C['coral'])
        w = 38 if label in ('AI',) else 44
        return f'<rect x="{x}" y="{cy - 9}" width="{w}" height="18" rx="9" fill="{col[0]}" stroke="{col[1]}"/>' + txt(x + w / 2, cy, label, 11, col[2], 'middle', '600'), w

    def card(x, y, w, name, col, rows):
        f, s, t, _ = C[col]
        h = 40 + len(rows) * 28 + 14
        r = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#FFFFFF" stroke="{s}" stroke-width="1"/>',
             f'<path d="M{x} {y + 10} a10 10 0 0 1 10 -10 h{w - 20} a10 10 0 0 1 10 10 v30 h-{w} z" fill="{f}" stroke="{s}" stroke-width="1"/>',
             txt(x + 16, y + 20, f'{name} ({len(rows)})', 15, t, 'start', '600')]
        for i, (tn, desc, bs) in enumerate(rows):
            cy = y + 61 + i * 28
            r.append(txt(x + 18, cy, tn, 13, '#2C2C2A', 'start', '400', M))
            r.append(txt(x + 228, cy, desc, 13, '#444441'))
            bx = x + w - 16
            for b in reversed(bs):
                _, bw = badge(0, cy, b)
                bx -= bw
                r.append(badge(bx, cy, b)[0])
                bx -= 6
        return ''.join(r), h

    ly = 84
    for g in GROUPS[:4]:
        s, h = card(30, ly, 560, *g)
        o.append(s)
        ly += h + 16
    ry = 84
    for g in GROUPS[4:]:
        s, h = card(610, ry, 560, *g)
        o.append(s)
        ry += h + 16
    yy = ly + 6
    for i, (col, label) in enumerate([('purple', 'AI 배지: AI를 호출하는 툴 (나머지는 AI 없음)'), ('coral', '주제 배지: 환경 차이 흡수·교차 검증 전용'),
                                      ('teal', 'SSL 배지: HTTPS/TLS 적용·검증 (클라우드 필수, 로컬 보류)'), ('gray', '공통·운영')]):
        f, s, _, _ = C[col]
        y = yy + i * 26
        o.append(f'<rect x="30" y="{y}" width="14" height="14" rx="3" fill="{f}" stroke="{s}"/>')
        o.append(txt(52, y + 7, label, 13))
    H2 = max(ry, yy + 110) + 10
    open('02_component-tools.svg', 'w').write(svg(W, H2, o))


# ---------- 04 단계 매핑 ----------
P = ('#EEEDFE', '#534AB7', '#3C3489'); T = ('#E1F5EE', '#0F6E56', '#085041'); G = ('#F1EFE8', '#5F5E5A', '#2C2C2A')
ROWS04 = [
    ('①', '딸깍 (트리거)', G, ['챗봇 "배포해줘"', '+ 로컬 디렉토리 경로 또는 깃 주소'], ['코드 스냅샷 zip'], ['S3에 업로드 (빌드 소스)'], 'C3 · O2', '없음'),
    ('①', '분석 (인지)', P, ['환경변수·SQLite·로컬 파일·localhost 인지', '→ deploy.yaml 초안 (규칙 + Jev)'], ['–'], ['–'], 'O2', 'Jev'),
    ('①', '계획', P, ['필요한 단계·순서를 계획 JSON으로'], ['–'], ['–'], 'O2', 'LLM'),
    ('①', '이식성 패치', P, ['SQLite → 공용 SQL, localhost → 환경변수', '업로드 → 저장소 어댑터, 프록시 헤더 대응'], ['(토글 ON일 때)'], ['(토글 ON일 때)'], 'O3', 'LLM'),
    ('②', '계획 검증', T, ['스키마·허용 툴·필수 단계·토글 검사', '불합격 → 재지시 1회 → 규칙 계획'], ['–'], ['–'], 'O2', '없음'),
    ('③', '빌드', T, ['한 번 빌드, 같은 이미지(digest)를', '두 환경에서 사용'], ['ECR에서 같은 이미지 받기'], ['CodeBuild → ECR'], 'C2 (C1)', '없음'),
    ('④', '설정·시크릿', T, ['인지한 환경변수를 대상별로 주입', '(비밀값은 사람 승인, AI는 값을 안 봄)'], ['.env / Compose secrets'], ['Secrets Manager 참조'], 'O1 / C2', '없음'),
    ('④', 'DB', T, ['SQLite 앱을 운영형 DB에 연결 (TLS)', '스키마 생성 + (선택) 데이터 이관'], ['DB 컨테이너', '(RDS와 같은 엔진)'], ['RDS (미리 생성, TLS 강제)', '(sslmode=verify-full)'], 'O1 / C2', '없음'),
    ('④', '파일 저장소', T, ['로컬 디스크 업로드를', '객체 저장소로'], ['MinIO 컨테이너'], ['S3 버킷'], 'O1 / C2', '없음'),
    ('④', 'SSL/TLS', T, ['클라우드 공개 접근은 HTTPS, HTTP → HTTPS 301', 'TLS 1.2 이상 (TLS 1.3 정책 명시)'], ['보류', '(http://localhost:8080)'], ['ACM + ALB 443', '(관리 페이지 도메인)'], 'C1', '없음'),
    ('④', '배포 실행', T, ['로컬(스테이징) 먼저, 스모크 통과 후 클라우드', '실행기가 계획 순서대로 호출'], ['docker compose up'], ['ECS 서비스 업데이트', '(D ≤ 60초)'], 'O1 / C2', '없음'),
    ('⑤', '검증', T, ['헬스·스모크·TLS(클라우드) → 교차 비교(같은 요청)', '불일치 → 규칙 롤백 → 원인 분석'], ['http://localhost:8080 대상', '(AI 패치의 관문, TLS 검사 없음)'], ['https://<관리 페이지 도메인> 대상'], 'O3 · C3', '없음*'),
    ('⑤', '보고', P, ['결과 카드: 로컬 http·클라우드 HTTPS URL·단계별 초', 'AI가 추가한 단계·AI 비용·작업 로그'], ['–'], ['–'], 'C3 · O1', 'LLM(선택)'),
]


def img04():
    W = 1440
    x0 = 20
    cols = [(x0, 190), (x0 + 200, 420), (x0 + 630, 270), (x0 + 910, 270), (x0 + 1190, 110), (x0 + 1310, 90)]
    top = 110
    rh = 60
    H = top + len(ROWS04) * (rh + 8) + 116
    o = [txt(x0, 32, '딸깍 한 번이 지나가는 단계와 개발 담당: 플랜 → 검증 → 빌드 → 배포 → 검증 및 보고', 21, '#2C2C2A', 'start', '600'),
         txt(x0, 60, '같은 기능은 공통 로직, 환경마다 다른 부분은 로컬/클라우드 어댑터. 실행(빌드·배포·검증·롤백)은 AI 없음. 테스트 단계 제외(로컬 스모크가 관문).', 13, '#5F5E5A')]
    heads = ['단계', '공통 로직 (환경 무관)', '로컬(온프레미스) 어댑터', '클라우드 어댑터', '담당', 'AI 사용']
    for (cx, cw), h in zip(cols, heads):
        o.append(f'<rect x="{cx}" y="{top - 34}" width="{cw}" height="28" rx="6" fill="#F1EFE8"/>')
        o.append(txt(cx + cw / 2, top - 20, h, 13, '#2C2C2A', 'middle', '600'))
    for i, (n, stage, col, common, loc, cld, owner, ai) in enumerate(ROWS04):
        y = top + i * (rh + 8)
        f, s, t = col
        cx, cw = cols[0]
        o.append(f'<rect x="{cx}" y="{y}" width="{cw}" height="{rh}" rx="8" fill="{f}" stroke="{s}"/>')
        o.append(txt(cx + 14, y + rh / 2, n, 15, s, 'start', '600'))
        o.append(txt(cx + 44, y + rh / 2, stage, 14, t, 'start', '600'))

        def cell(ix, lines, fill, stroke, color):
            cx, cw = cols[ix]
            o.append(f'<rect x="{cx}" y="{y}" width="{cw}" height="{rh}" rx="8" fill="{fill}" stroke="{stroke}"/>')
            if len(lines) == 1:
                o.append(txt(cx + 14, y + rh / 2, lines[0], 13, color))
            else:
                o.append(txt(cx + 14, y + rh / 2 - 10, lines[0], 13, color))
                o.append(txt(cx + 14, y + rh / 2 + 10, lines[1], 13, color))
        cell(1, common, '#FFFFFF', s, '#2C2C2A')
        for ix, v in ((2, loc), (3, cld)):
            d = v == ['–'] or v[0] == '보류'
            cell(ix, v, '#FAFAF7' if d else '#FFFFFF', '#D3D1C7' if d else '#0F6E56', '#888780' if d else '#085041')
        cx, cw = cols[4]
        o.append(f'<rect x="{cx}" y="{y}" width="{cw}" height="{rh}" rx="8" fill="#F1EFE8"/>')
        o.append(txt(cx + cw / 2, y + rh / 2, owner, 13, '#2C2C2A', 'middle', '600'))
        cx, cw = cols[5]
        aic = PU = ('#EEEDFE', '#534AB7', '#3C3489') if ai not in ('없음', '없음*') else ('#FFFFFF', '#D3D1C7', '#5F5E5A')
        o.append(f'<rect x="{cx}" y="{y}" width="{cw}" height="{rh}" rx="8" fill="{aic[0]}" stroke="{aic[1]}"/>')
        o.append(txt(cx + cw / 2, y + rh / 2, ai, 12.5, aic[2], 'middle', '600'))
    ly = top + len(ROWS04) * (rh + 8) + 18
    for j, (c, lab) in enumerate([(P, 'AI가 판단·생성하는 단계'), (T, '결정적으로 실행되는 단계 (AI 없음)'), (G, '입력')]):
        f, s, _ = c
        x = x0 + j * 300
        o.append(f'<rect x="{x}" y="{ly}" width="14" height="14" rx="3" fill="{f}" stroke="{s}"/>')
        o.append(txt(x + 22, ly + 7, lab, 13, '#5F5E5A'))
    o.append(txt(x0, ly + 36, '담당: C1 인프라 · C2 클라우드 어댑터 · C3 클라우드 검증·관리 페이지 · O1 온프렘·실행기 · O2 분석·계획 · O3 패치·교차 검증', 12, '#5F5E5A'))
    o.append(txt(x0, ly + 58, '* 검증 판정·롤백은 AI 없음. 실패·불일치가 있을 때만 원인 분석(diagnose_parity_gap)에서 Jev + LLM 사용', 12, '#5F5E5A'))
    open('04_pipeline-stages-roles.svg', 'w').write(svg(W, H, o))


# ---------- 05 관리 페이지 ----------
def img05():
    PU = ('#EEEDFE', '#534AB7', '#3C3489'); TE = ('#E1F5EE', '#0F6E56', '#085041'); GR = ('#F1EFE8', '#5F5E5A', '#2C2C2A'); CO = ('#FAECE7', '#993C1D', '#712B13')

    def rect(x, y, w, h, fill, stroke, rx=8):
        return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="1"/>'

    def chip(x, cy, label, col, w=None):
        w = w or (14 + len(label) * 11)
        return rect(x, cy - 10, w, 20, col[0], col[1], 10) + txt(x + w / 2, cy, label, 11, col[2], 'middle', '600'), w

    LG = ('#FAFAF7', '#B4B2A9', '#888780')  # 보류(해당 없음)
    W, H = 1200, 852
    o = [rect(20, 16, 1160, 50, GR[0], GR[1], 10), txt(40, 41, '관리 페이지 (배포 대상 앱과 별개, 화면 예시)', 16, '#2C2C2A', 'start', '600'),
         txt(390, 41, '프로젝트: photo-board', 13, '#5F5E5A'), txt(560, 41, '클라우드 도메인: (관리 페이지 입력값)', 13, '#2C2C2A', 'start', '600'),
         txt(960, 41, '코드 수정 토글', 13, '#2C2C2A'),
         rect(1068, 29, 48, 24, PU[1], PU[1], 12), '<circle cx="1104" cy="41" r="9" fill="#FFFFFF"/>', txt(1126, 41, 'ON', 12, PU[2], 'start', '600')]

    def panel(x, y, w, h, title, tag):
        r = [rect(x, y, w, h, '#FFFFFF', '#B4B2A9', 10), txt(x + 16, y + 20, title, 14, '#2C2C2A', 'start', '600')]
        s, _ = chip(x + w - 16 - (14 + len(tag) * 11), y + 20, tag, PU)
        r.append(s)
        r.append(f'<line x1="{x}" y1="{y + 38}" x2="{x + w}" y2="{y + 38}" stroke="#E8E6DE"/>')
        return ''.join(r)

    def bubble(bx, by, bw, lines, user=False, col=None):
        fill, stroke, tc = (('#534AB7', '#534AB7', '#FFFFFF') if user else (col or ('#F1EFE8', '#D3D1C7', '#2C2C2A')))
        bh = 16 + len(lines) * 20
        r = [rect(bx, by, bw, bh, fill, stroke, 12)]
        for i, l in enumerate(lines):
            r.append(txt(bx + 12, by + 18 + i * 20, l, 12.5, tc))
        return ''.join(r)

    # 설정: 도메인 (데모 전 한 번, 사람만 수정)
    sy = 78
    o.append(rect(20, sy, 1160, 52, '#FFFFFF', '#B4B2A9', 10))
    o.append(txt(36, sy + 26, '설정 (도메인)', 14, '#2C2C2A', 'start', '600'))
    o.append(chip(140, sy + 26, '사람만 수정', CO, 76)[0])
    o.append(txt(240, sy + 26, 'DNS 방식', 12, '#888780'))
    o.append(chip(300, sy + 26, 'Route 53 · 자동', TE, 112)[0])
    o.append(chip(420, sy + 26, '외부 DNS · CNAME 안내', LG, 146)[0])
    o.append(txt(596, sy + 26, '인증서', 12, '#888780'))
    o.append(chip(642, sy + 26, '발급됨 · 사용 중', TE, 112)[0])
    o.append(txt(778, sy + 26, 'AI는 읽기만 · 데모 전날까지 사전 발급', 12, '#5F5E5A'))
    o.append(rect(1056, sy + 12, 108, 28, PU[1], PU[1], 8))
    o.append(txt(1110, sy + 26, '도메인 연결', 12.5, '#FFFFFF', 'middle', '600'))
    x, y, w, h = 20, 144, 370, 480
    o.append(panel(x, y, w, h, '챗봇', '① 입력 · 승인'))
    o.append(bubble(x + w - 196, y + 54, 180, ['배포해줘', '~/projects/photo-board'], True))
    o.append(bubble(x + 14, y + 118, 300, ['분석 완료: 환경변수 2 · SQLite 1', '업로드 폴더 1 · localhost 1']))
    o.append(bubble(x + 14, y + 186, 300, ['계획 확정 (AI가 추가한 단계 표시)', '② 계획 검증 통과']))
    o.append(bubble(x + 14, y + 254, 300, ['패치 3건 생성 (DB·업로드·주소)', 'diff 보기'], col=(PU[0], PU[1], PU[2])))
    o.append(rect(x + 14, y + 310, 300, 34, '#FFFFFF', PU[1], 8))
    o.append(txt(x + 26, y + 327, '승인', 12.5, PU[2], 'start', '600'))
    o.append(txt(x + 90, y + 327, '거절', 12.5, '#5F5E5A'))
    o.append(bubble(x + 14, y + 356, 300, ['SECRET_KEY가 클라우드 시크릿에', '없습니다. 값을 입력해 주세요'], col=(CO[0], CO[1], CO[2])))
    o.append(rect(x + 14, y + 426, 300, 34, '#FFFFFF', CO[1], 8))
    o.append(txt(x + 26, y + 443, '값 입력 후 승인 (AI는 값을 보지 않음)', 12, CO[2]))
    x, y, w, h = 406, 144, 370, 480
    o.append(panel(x, y, w, h, '계획', '① 플랜 · ② 검증'))
    steps = [('patch_db_access', '', 'AI'), ('patch_storage', '', 'AI'), ('patch_config', '', 'AI'), ('build_image', '', '규칙'), ('prepare_db', 'local', '규칙'),
             ('deploy_tier', 'local', '규칙'), ('smoke_test', 'local', '규칙'), ('sync_env_to_cloud', 'cloud', 'AI'),
             ('ensure_tls', 'cloud', '규칙'), ('deploy_tier', 'cloud', '규칙'), ('verify_tls', 'cloud', '규칙')]
    for i, (tn, tg, by) in enumerate(steps):
        cy = y + 60 + i * 30
        o.append(txt(x + 16, cy, tn, 12.5, '#2C2C2A', 'start', '400', M))
        if tg:
            o.append(txt(x + 190, cy, tg, 12, '#888780'))
        s, _ = chip(x + w - 16 - 52, cy, by, PU if by == 'AI' else TE, 52)
        o.append(s)
    o.append(txt(x + 16, y + 410, '칩 = 이 단계를 계획에 넣은 쪽(규칙/AI). 실행은 전부 코드', 11.5, '#888780'))
    o.append(rect(x + 14, y + h - 54, w - 28, 40, TE[0], TE[1], 8))
    o.append(txt(x + 26, y + h - 34, '② 계획 검증 통과: 허용 툴 · 필수 단계 · 토글', 12.5, TE[2], 'start', '600'))
    x, y, w, h = 792, 144, 388, 480
    o.append(panel(x, y, w, h, '진행', '③ ~ ⑤'))
    o.append(txt(x + 16, y + 56, '단계', 12, '#888780'))
    o.append(txt(x + 150, y + 56, '로컬(온프레미스)', 12, '#888780'))
    o.append(txt(x + 280, y + 56, '클라우드', 12, '#888780'))
    prog = [('③ 빌드', 'CodeBuild 42초', 'span', TE), ('④ 설정·DB', ('완료 5초', TE), ('완료 9초', TE), None), ('④ 저장소', ('완료 2초', TE), ('완료 1초', TE), None),
            ('④ SSL/TLS', ('보류', LG), ('재사용 3초', TE), None), ('④ 배포', ('완료 9초', TE), ('진행 중 38초', PU), None), ('⑤ 스모크·TLS', ('스모크 통과 3초', TE), ('대기', GR), None),
            ('⑤ 교차 비교', '대기', 'span', GR)]
    for i, row in enumerate(prog):
        cy = y + 90 + i * 44
        o.append(txt(x + 16, cy, row[0], 13, '#2C2C2A', 'start', '600'))
        if row[2] == 'span':
            s, _ = chip(x + 150, cy, row[1], row[3], 220)
            o.append(s)
        else:
            s, _ = chip(x + 150, cy, row[1][0], row[1][1], 110)
            o.append(s)
            s, _ = chip(x + 280, cy, row[2][0], row[2][1], 96)
            o.append(s)
    o.append(txt(x + 16, y + h - 72, '로컬 SSL/TLS는 보류(해당 없음), 스모크만 실행', 12, '#5F5E5A'))
    o.append(txt(x + 16, y + h - 50, '클라우드 배포를 기다리는 동안 로컬 결과를 먼저 보여줌', 12, '#5F5E5A'))
    o.append(txt(x + 16, y + h - 28, '(수치는 예시)', 12, '#888780'))
    x, y, w, h = 20, 640, 1160, 196
    o.append(panel(x, y, w, h, '결과 카드', '⑤ 보고'))
    o.append(txt(x + 16, y + 62, '접속 URL', 12, '#888780'))
    o.append(txt(x + 16, y + 86, '로컬: http://localhost:8080 (HTTPS 보류)', 13, '#2C2C2A', 'start', '400', M))
    o.append(txt(x + 16, y + 110, '클라우드: https://<관리 페이지에 입력한 도메인>', 13, '#2C2C2A', 'start', '400', M))
    o.append(txt(x + 16, y + 138, 'TLS 1.3 · 발급자 Amazon · 남은 약 195일', 12.5, '#085041'))
    o.append(txt(x + 16, y + 160, 'HTTP → 301 · TLS 1.0/1.1 거부 (클라우드만)', 12.5, '#085041'))
    o.append(txt(x + 440, y + 62, '교차 검증', 12, '#888780'))
    for i, l in enumerate(['글 작성 → 목록 조회: 일치', '사진 업로드 → 다른 복제본 조회: 일치', '배포 버전: 두 환경 run-0930-01', 'TLS 검사: 클라우드 통과 (로컬 보류)']):
        o.append(txt(x + 440, y + 86 + i * 24, l, 13, '#085041'))
    o.append(txt(x + 810, y + 62, '요약', 12, '#888780'))
    for i, l in enumerate(['총 소요 시간 · AI가 추가한 단계', 'AI 호출 수 · 토큰 · 비용', '작업 로그 · 이유 보기 · 롤백 버튼']):
        o.append(txt(x + 810, y + 86 + i * 24, l, 13, '#2C2C2A'))
    open('05_admin-page-wireframe.svg', 'w').write(svg(W, H, o))


if __name__ == '__main__':
    img01(); img02(); img04(); img05()
    print('svg generated')
