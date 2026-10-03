# 수정11 provider 인계

공개 import: `ddak.core.ai.providers`. 웹은 직접 import하지 않고 app이 callback을 주입한다.

```python
provider_catalog(settings: Settings | None = None) -> list[dict[str, Any]]
provider_status(id: str, settings: Settings, *, role: ProviderRole | None = None) -> dict[str, Any]
test_provider_connection(id: str, settings: Settings, *, role: ProviderRole | None = None) -> dict[str, Any]
validate_provider_selection(id: str, role: ProviderRole) -> None
register_provider(spec: ProviderSpec) -> None
provider_key(settings: Settings, key_name: str | None) -> str | None
get_provider(settings: Settings) -> LLMProvider
get_jev_client(settings: Settings | None = None) -> JudgmentClient
```

- `ProviderRole = Literal["generation", "judgment"]`. 둘 다 선택한 provider는 역할별 검증에 `role`을 지정한다. 생략 시 선택한 판단 전용 provider는 judgment, 그 외 첫 지원 역할을 사용한다.
- catalog: `id,label,kind,roles,auth,default_model,models,key_name`; settings를 전달하면 비밀값 대신 `key_configured` 부울만 추가한다. provider/factory/runner는 metadata에 없다.
- status/test: `status=green|gray|red`, `detail`, 성공 시 ISO UTC `verified_at`. status는 AI 호출 없이 인증/설정과 프로세스 내 probe 기록만 조회한다. 키 있음·CLI 인증만으로 green이 되지 않는다. CLI 인증 조회는 `auth status --json`이며 원문·이메일을 내보내지 않는다.
- test는 UI의 명시적 연결 테스트 callback이다. 프롬프트/툴 이름을 받지 않고, 툴 문맥도 생성하지 않는다. fixed 최소 응답을 공통 redact/retry/schema gate로 검증한다. live 응답만 green이고 replay/fixture/cache/TypeSafe Jev는 gray다. 키·역할·모델·CLI/effort가 다르면 기존 연결 기록은 쓰지 않는다.
- Settings: `llm_provider`, `judgment_provider`, `judgment_model`, `anthropic_api_key`, `provider_keys: Mapping[str,str]`(repr 제외). main은 generation_provider→llm_provider 등을 replace로 주입한다. provider_keys가 우선이며 기존 anthropic_api_key/groq_api_key는 fallback이다. 명시적 빈 mapping 값은 fallback을 끈다. 임의 provider key_name은 새로운 Settings 필드를 요구하지 않는다. registry는 vault를 읽지 않는다.
- 신규 provider 파일은 `complete(AIRequest)->AIResponse`를 구현하고 `register_provider(ProviderSpec(..., generation_factory=lambda cfg: CustomProvider(provider_key(cfg, "custom_key"))))` 한 번으로 등록한다. 판단 factory를 생략하면 공통 JSON 판단 어댑터를 재사용한다. 별도 `judgment_factory`는 `ask(state=..., questions=...)->list[JevAnswer]`와 성공 출처 `source`를 제공한다. `status` hook은 상태만 조회하고, `test` hook은 fixed probe가 통과한 AIResponse에 추가 검증만 적용한다.
- `claude-cli`/`claude-api` 기본 모델은 `claude-sonnet-5-5`, effort는 기존 low. Groq는 API만 지원하며 설정 모델을 사용한다. Jev는 미연결 표시만 하고 키를 외부에 보내지 않는다. Codex 구현은 추가하지 않았다.
- **API legacy=Groq alias migration**: `llm_backend=api`는 계속 Groq이며 기존 llm_api_key/llm_model을 사용한다. `api.py`는 공통 인터페이스·호환 export, `groq_api.py`는 Groq, `anthropic_api.py`는 Claude Messages다. 기존 잘못된 `api.AnthropicApiProvider`는 deprecated alias로 GroqApiProvider를 가리킨다. 실제 provider 이름은 groq이고 source는 공통 live/replay 계약을 유지한다. 구버전 계약 테스트의 API `.name == "api"` 기대값은 `.name == "groq"`로 바꿔야 한다(이 테스트 파일은 C 수정 범위 밖).

검증은 fakeHTTP/fake runner/provider만 사용한다. 실제 AI/CLI 실행 검증은 수행하지 않았다.
