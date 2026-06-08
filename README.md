# 🐋 Wavb — 음성 기반 지식 관리 서비스

> 강의를 실시간으로 녹음하고, 지식으로 변환하는 AI 튜터 앱

![Status](https://img.shields.io/badge/Status-In%20Progress-teal)
![Period](https://img.shields.io/badge/Period-2026.05~-lightgrey)
![Dev](https://img.shields.io/badge/Dev-Solo-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![React Native](https://img.shields.io/badge/React%20Native-61DAFB?logo=react&logoColor=black)
![LangGraph](https://img.shields.io/badge/LangGraph-1C3C3C?logo=langchain&logoColor=white)

---

## 📌 프로젝트 개요

학생들은 강의 중 필기 부담, 탐색 비효율, 질문 장벽, 그리고 녹음 파일을 결국 듣지 않는 문제를 겪습니다.

**Wavb**는 강의 음성을 실시간으로 텍스트화하고, RAG 기반 AI 챗봇으로 즉시 질문하며, 지식 그래프로 학습 상태를 시각화하는 **AI 튜터 앱**입니다.

---

## 🎯 타겟 사용자

| 대상 | 활용 목적 |
|------|----------|
| 중·고·대학생 | 필기 부담 감소, 시험 대비 복습, 강의 체계적 관리 |
| 취업 준비생 | 인강 학습, 기술 면접 준비, 학습 내용 정리 |

---

## ✨ 주요 기능

### 1. 실시간 STT
- Deepgram Nova-3 WebSocket 기반 실시간 음성 스크립트 변환
- `is_final` 파라미터 활용으로 API 호출 비용 최적화

### 2. 스크립트 검색
- 생성 중인 스크립트 내 키워드 실시간 탐색
- 강의 중 놓친 내용을 즉시 확인하고 원하는 구간으로 이동

### 3. RAG 챗봇
- **LangGraph Adaptive RAG** 기반 질의응답
- 질문 유형에 따라 4가지 검색 툴 자동 분기
  - 벡터 유사도 검색 → 개념 질문 ("정규화가 뭔가요?")
  - 메타데이터 검색 → 위치 질문 ("몇 강에서 나왔나요?")
  - 멀티파일 검색 → 비교 질문 ("1강이랑 3강 설명이 달랐나요?")
  - 웹 검색 (Tavily) → 외부 정보 질문 ("최신 GPT 모델은?")
- **강사 페르소나 자동 추출**: STT 결과에서 말투·설명 방식·표현 습관을 분석해 답변에 반영
- 1차 Fallback 재검색 → 2차 불만족 시 사용자 선택지 제공

### 4. 지식 그래프
- 업로드 파일 기반 지식 그래프 자동 생성
- 자주 질문한 개념 노드 강조 표시
- 퀴즈 출제 및 정답 여부 시각화

---

## 🏗️ 시스템 아키텍처

```
React Native (Android)
        │
        │ HTTPS / WebSocket
        ▼
┌─────────────────────────────────────────┐
│            Railway Platform             │
│                                         │
│  ┌──────────────────────────────────┐   │
│  │            FastAPI               │   │
│  └──────────────────────────────────┘   │
│       │           │          │          │
│  실시간 STT    파일 처리   LangGraph    │
│  Deepgram    Whisper→임베딩  Agent      │
│                                         │
│  ┌──────────────────────────────────┐   │
│  │   PostgreSQL + pgvector          │   │
│  │         (Supabase)               │   │
│  └──────────────────────────────────┘   │
└─────────────────────────────────────────┘
        │
        │ External API
        ▼
  GPT-4o-mini  │  Tavily API  │  OpenAI Embedding
  Deepgram API │  Whisper API
```

---

## 🛠️ 기술 스택

| 분류 | 기술 |
|------|------|
| **Backend** | FastAPI, LangGraph, Python |
| **Frontend** | React Native (Android) |
| **AI/ML** | Deepgram Nova-3, OpenAI Whisper, GPT-4o-mini, text-embedding-3-small |
| **Database** | PostgreSQL + pgvector (Supabase) |
| **검색** | Tavily API (웹 검색) |
| **배포** | Railway |

---

## 🔧 기술적 도전과 해결

### 문제: 50분 오디오 처리 시 3분 이상 소요

**목표: 1분 이내 응답**

30분 오디오 기준 벤치마크 결과:

| 설정 | 청크 수 | API 호출 | 처리 시간 | 비고 |
|------|---------|---------|---------|------|
| 300초 · 동시요청 2 | 7개 | 7번 | **56.6초** ✅ | 평상시 |
| 300초 · 동시요청 1 | 7개 | 7번 | 102.1초 ❌ | |
| 600초 · 동시요청 2 | 4개 | 4번 | **66.2초** ✅ | 부하 시 |
| 600초 · 동시요청 1 | 4개 | 4번 | 104.3초 ❌ | |

**해결 전략:**
- 동시 요청 수를 줄이는 대신 **청크 크기 증가** 방식 채택
- Whisper Tier 1 RPM=50 제한 고려: 1분 내 동시 사용자 25명 초과 시 요청량 초과 가능
- **적응형 청크 전략 적용**
  - Default: 청크 300초 + 동시 요청 수 2
  - RPM 40 초과 시: 청크 300초 → 600초 자동 전환

---

## 📊 차별화 포인트

| 기능 | 일반 STT 서비스 | Wavb |
|------|:-----------:|:----:|
| 실시간 음성 기록 | ✅ | ✅ |
| 실시간 스크립트 검색 | ❌ | ✅ |
| RAG 질의응답 | ❌ | ✅ |
| 강사 페르소나 답변 | ❌ | ✅ |
| 지식 그래프 | ❌ | ✅ |
| 복습 문제 생성 | ❌ | ✅ |

---

## 🗺️ 개발 현황 및 로드맵

**현재 (진행 중)**
- [x] MVP 개발 완료
- [x] 핵심 기능 테스트 및 고도화
- [ ] Supabase + Railway 배포 진행 중
- [ ] 개발자 콘솔 등록

**향후 계획**
- [ ] 지식 그래프 + 퀴즈 기능 개발 및 통합
- [ ] Android 정식 출시

---

## 📁 프로젝트 구조

```
wavb/
├── app/
│   ├── api/            # FastAPI 라우터
│   ├── services/       # 비즈니스 로직
│   │   ├── stt/        # Deepgram 실시간 STT
│   │   ├── rag/        # LangGraph RAG 에이전트
│   │   └── whisper/    # 파일 기반 STT + 임베딩
│   └── models/         # DB 모델
├── mobile/             # React Native 앱
└── README.md
```

---
