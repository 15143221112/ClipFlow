# 📥 ClipFlow

> 도우인 크리에이터 홈 → 일괄 수집 → MP4 다운로드 → 음성 전사(ASR) → 텍스트 보정 → 페이수 Bitable 쓰기.
> 도우인 영상을 구조화된 텍스트로 바꾸는 웹 기반 자동화 파이프라인.

[English](README.en.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

## 특징
- **전체 자동화**: 크리에이터 홈 또는 단일 영상 링크 입력 → 수집 → 다운로드 → 오디오 추출 → 전사 → 용어 보정 → 페이수 Bitable 쓰기까지 자동.
- **로컬 우선**: 영상·오디오·전사 결과를 모두 로컬(SQLite + 파일)에 저장. 쿠키 만료 후에도 재처리 가능.
- **3중 ASR**: 로컬 Whisper / iFlytek / 페이수 전환 가능(기본 로컬).
- **LLM 보정**: 구두점 추가 및 전문 용어 동음오류를 LLM(알리 qwen-max 또는 로컬 vLLM)으로 보정.
- **조건 조회 + 선택 내보내기**: 상태·작성자·날짜·키워드 등 필터 후 **현재 검색 결과만** CSV로 내보내기.
- **DB 영속화**: 작품과 설정을 SQLite(videos/settings)에 저장, 설정은 페이지에서 즉시 반영.
- **페이수 Bitable 연동**: 필드 자동 생성, aweme_id 기준 upsert(존재 시 업데이트).

## 빠른 시작
```bash
git clone https://github.com/<you>/ClipFlow.git && cd ClipFlow
cp settings.example.json settings.json
docker compose up -d --build        # 또는: pip install -r requirements.txt && uvicorn app:app --port 9009
# 브라우저에서 http://<host>:9009
```

## 라이선스
[MIT](LICENSE). 도우인 플랫폼 규칙과 현지 법률을 준수한 합법적인 용도로만 사용하세요.
