# 긴 오디오 청킹 + 병렬 STT 처리 계획

## Summary
- 방식은 **타당함**: 긴 오디오를 작은 청크로 나누고, 각 청크를 제한 병렬로 STT 처리한 뒤 순서대로 병합하면 단일 요청의 파일 크기 제한과 처리 시간을 줄일 수 있다.
- 단, “청크를 전부 무제한 병렬 처리”는 위험하다. OpenAI rate limit은 요청 수, 토큰, 오디오 사용량 등 여러 기준으로 걸릴 수 있으므로 bounded concurrency가 필요하다. ([developers.openai.com](https://developers.openai.com/api/docs/guides/rate-limits))
- OpenAI Speech-to-text 파일 업로드는 현재 25MB 제한이 있고, `mp3`, `mp4`, `mpeg`, `mpga`, `m4a`, `wav`, `webm`을 지원하므로 서버에서 청크를 25MB 미만으로 만들어 보내는 설계가 맞다. ([developers.openai.com](https://developers.openai.com/api/docs/guides/speech-to-text))

## Step 1. 오디오 분석 계층 추가
- 필요성: 긴 파일을 무조건 한 번에 OpenAI로 보내면 25MB 제한 또는 처리 시간 문제에 걸릴 수 있다.
- 작업:
  - 업로드 파일을 임시 디렉터리에 저장한다.
  - `ffmpeg` 또는 Python dependency 기반 ffmpeg binary로 전체 duration을 측정한다.
  - 파일이 비어 있거나 decode 불가하면 400 응답을 반환한다.
  - 현재 서버에 시스템 `ffmpeg`가 없을 수 있으므로 `imageio-ffmpeg` 같은 Python dependency를 사용한다.

## Step 2. Duration 기반 청크 계획 수립
- 필요성: 고정 청크 크기보다 오디오 길이에 따라 청크 수와 병렬 처리량을 조절해야 요청 시간을 줄일 수 있다.
- 작업:
  - 설정값을 추가한다:
    - `AUDIO_TRANSCRIPTION_CONCURRENCY=3`
    - `AUDIO_CHUNK_MIN_SECONDS=300`
    - `AUDIO_CHUNK_MAX_SECONDS=900`
    - `AUDIO_CHUNK_OVERLAP_SECONDS=2`
    - `AUDIO_TARGET_CHUNK_MAX_MB=24`
    - `AUDIO_SYNC_TIMEOUT_BUDGET_SECONDS=110`
  - 기본 청크 크기:
    ```text
    chunk_seconds = clamp(ceil(duration_seconds / concurrency), 300, 900)
    ```
  - 각 청크는 앞뒤 문맥 손실을 줄이기 위해 2초 overlap을 둔다.
  - mp3 mono, 16kHz, 낮은 bitrate로 변환해 청크당 24MB 이하를 목표로 한다.

## Step 3. 제한 병렬 STT 처리
- 필요성: 병렬 처리는 속도를 개선하지만, 무제한 병렬은 rate limit과 provider 실패를 악화시킬 수 있다.
- 작업:
  - `asyncio.Semaphore(AUDIO_TRANSCRIPTION_CONCURRENCY)`로 STT 동시 요청 수를 제한한다.
  - 각 청크는 최대 1회 재시도한다.
  - 재시도 후 실패하면 전체 요청은 502로 실패시키고 실패한 chunk index를 응답 detail에 포함한다.
  - `whisper-1` 사용 시 `verbose_json`으로 segment timestamp를 받아 overlap 중복 제거에 활용한다. `whisper-1`은 `verbose_json` 출력 형식을 지원한다. ([developers.openai.com](https://developers.openai.com/api/docs/guides/speech-to-text))

## Step 4. Transcript 병합 및 중복 최소화
- 필요성: 청크별 STT 결과를 단순 연결하면 overlap 구간 문장이 중복될 수 있다.
- 작업:
  - 청크 결과는 반드시 `chunk_index` 순서로 병합한다.
  - 첫 청크를 제외한 청크는 `leading_overlap_seconds` 안에 끝나는 segment를 제거한다.
  - segment 정보가 없는 모델을 사용할 경우에는 index 순서 연결만 수행한다.
  - 최종 API 응답의 `transcript`는 기존처럼 string으로 유지한다.

## Step 5. 긴 Transcript 요약 최적화
- 필요성: STT는 해결되어도 transcript가 길면 요약 모델 입력이 길어져 실패하거나 느려질 수 있다.
- 작업:
  - 설정값을 추가한다:
    - `SUMMARY_TEXT_CHUNK_CHARS=16000`
    - `SUMMARY_CONCURRENCY=2`
  - transcript가 기준 이하이면 기존 단일 요약을 사용한다.
  - 기준을 넘으면 텍스트를 나누어 부분 요약을 병렬 생성하고, 부분 요약들을 다시 최종 요약한다.
  - 최종 응답 shape는 유지한다:
    ```json
    {
      "transcript": "...",
      "summary": "..."
    }
    ```

## Step 6. API 동작 흐름
```text
POST /audio/summarize
→ 업로드 파일 검증
→ 임시 파일 저장
→ duration 측정
→ duration 기반 chunk_seconds 계산
→ ffmpeg로 mp3 청크 생성
→ 청크 STT 제한 병렬 처리
→ chunk_index 순서로 transcript 병합
→ 긴 transcript면 계층형 요약
→ { transcript, summary } 반환
```

## Test Plan
- 5분, 30분, 90분 오디오 duration에 대해 chunk size 계산이 기대값과 일치하는지 테스트한다.
- overlap이 적용된 chunk plan이 올바른 start/end 값을 만드는지 테스트한다.
- ffmpeg 호출부는 mock으로 처리하고 청크 생성 실패, decode 실패, 빈 파일 케이스를 테스트한다.
- STT 병렬 처리에서 동시 실행 수가 설정값을 넘지 않는지 테스트한다.
- 청크 결과가 완료 순서가 아니라 `chunk_index` 순서로 병합되는지 테스트한다.
- 한 청크가 실패 후 재시도 성공하는 케이스와, 재시도 후에도 실패해 502를 반환하는 케이스를 테스트한다.
- 긴 transcript는 부분 요약 후 최종 요약 경로를 타고, 짧은 transcript는 기존 단일 요약 경로를 타는지 테스트한다.
- 기존 `POST /audio/summarize` 응답 shape가 유지되는지 API 테스트로 확인한다.

## Assumptions
- 이번 단계에서는 기존 동기 API를 유지한다.
- 요청 시간은 duration 기반 청크 크기와 제한 병렬 처리로 줄이되, 매우 긴 파일의 HTTP timeout을 완전히 보장하지는 않는다.
- 매우 긴 파일을 안정적으로 처리하는 `job_id` 기반 비동기 API는 다음 단계에서 별도 기능으로 설계한다.
- `PLAN.md`는 인코딩이 깨져 있지만, 확인 가능한 규칙인 step 단위 계획, 필요성/이유, 작업 요약, 전체 동작 흐름을 반영했다.
