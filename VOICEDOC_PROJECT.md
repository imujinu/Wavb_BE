# VoiceDoc — 음성 문서화 서비스 프로젝트 설계서

> **Codex / AI 코딩 어시스턴트용 프로젝트 컨텍스트 문서**  
> 작성일: 2025-05-20 | 배포 목표: 2025-06-06

---

## 1. 프로젝트 개요

### 프로젝트명
레코닥 : Recod + Document

### 서비스 정의
사용자의 음성을 녹음하고, 실시간으로 텍스트(스크립트)로 변환한 뒤, AI가 자동으로 요약·퀴즈·검색 기능을 제공하는 **음성 문서화 관리 앱**

### 핵심 사용 흐름
```
음성 녹음 → STT 변환 → 스크립트 생성 → 요약/퀴즈 자동 생성 → 임베딩 저장 → 의미 검색
```

---

## 2. 기능 명세

### 우선순위 정의
| 우선순위 | 설명 | 포함 여부 |
|---------|------|---------|
| 높음 | v1 필수 기능 | ✅ 1차 스프린트 |
| 중간 | v1 포함 목표 | ✅ 1차 스프린트 |
| 낮음 | v1.1 목표 | ⚙️ 2차 스프린트 |
| 미정 | 추후 확장 | 🔲 추후 설계 |

### 기능 목록
| 카테고리 | 기능 | 우선순위 |
|---------|------|---------|
| 음성 녹음 | 사용자 음성 녹음 | 높음 |
| 음성 녹음 | 녹음된 음성을 파일로 변환 | 높음 |
| 음성 녹음 | 녹음되는 음성을 실시간으로 변환 | 높음 |
| 음성 녹음 | 녹음되는 음성을 실시간 스크립트로 생성 | 높음 |
| 음성 녹음 | 실시간 스크립트 편집 가능 | 중간 |
| 음성 녹음 | 스크립트 변환에 대한 화자 구별 | 낮음 |
| 콘텐츠 생성 | 음성 파일에 대한 요약본 생성 | 높음 |
| 콘텐츠 생성 | 음성 파일에 대한 퀴즈 생성 | 낮음 |
| 콘텐츠 생성 | 음성 파일 길이 편집 기능 | 미정 |
| 콘텐츠 생성 | 퀴즈에 대한 라벨링 | 미정 |
| 콘텐츠 생성 | 퀴즈에 대한 복습 | 미정 |

---

## 3. 기술 스택

### 프론트엔드 (모바일 앱)
```
Framework:    React Native + Expo (SDK 51+)
언어:         TypeScript
상태 관리:    Zustand
네비게이션:   Expo Router (파일 기반)
UI 컴포넌트:  React Native Paper 또는 NativeWind (Tailwind)
음성 녹음:    expo-av (Audio.Recording)
파일 관리:    expo-file-system
```

### 백엔드 (AI 처리 서버)
```
Framework:    FastAPI (Python 3.11+)
패키지 관리:  uv 또는 poetry
STT:          OpenAI Whisper API (실시간: whisper-1 streaming)
LLM/체인:    LangChain + OpenAI GPT-4o-mini
임베딩:       OpenAI text-embedding-3-small
벡터 DB:      Supabase pgvector (또는 Pinecone 대안)
배포:         Render.com 또는 Railway (무료 플랜)
```

### 데이터베이스 / 인프라
```
BaaS:         Supabase (PostgreSQL + pgvector + Storage + Auth)
파일 저장:    Supabase Storage (음성 파일 .m4a/.wav)
인증:         Supabase Auth (이메일 + OAuth)
```

### 배포 환경
```
Android:      Google Play Console (내부 테스트 → 프로덕션)
빌드:         EAS Build (Expo Application Services)
```

---



## 5. 데이터베이스 스키마 (Supabase PostgreSQL)

```sql
-- 사용자 (Supabase Auth와 연동)
CREATE TABLE profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id),
  email TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 녹음 파일
CREATE TABLE recordings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES profiles(id),
  title TEXT NOT NULL,
  file_path TEXT,           -- Supabase Storage 경로
  duration_seconds INTEGER,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 스크립트 (STT 결과)
CREATE TABLE transcripts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  recording_id UUID REFERENCES recordings(id),
  content TEXT NOT NULL,    -- 전체 스크립트
  is_edited BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 요약본
CREATE TABLE summaries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  recording_id UUID REFERENCES recordings(id),
  content TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 임베딩 벡터 (RAG용)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE embeddings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  recording_id UUID REFERENCES recordings(id),
  chunk_text TEXT NOT NULL,         -- 청크 단위 텍스트
  embedding vector(1536),           -- OpenAI text-embedding-3-small 차원
  chunk_index INTEGER,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 벡터 검색 인덱스
CREATE INDEX ON embeddings USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);

-- 퀴즈 (2차 스프린트)
CREATE TABLE quizzes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  recording_id UUID REFERENCES recordings(id),
  question TEXT NOT NULL,
  options JSONB,            -- ["A", "B", "C", "D"]
  answer TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 6. API 엔드포인트 명세

### FastAPI 서버 엔드포인트

```
POST /transcribe
  Body: { file: .m4a 파일 (multipart) }
  Return: { transcript: string, duration: number }

POST /transcribe/stream
  Body: { audio_chunk: bytes (WebSocket) }
  Return: WebSocket streaming { partial_transcript: string }

POST /summarize
  Body: { transcript_id: string, transcript: string }
  Return: { summary: string }

POST /embed
  Body: { recording_id: string, transcript: string }
  Return: { chunks_stored: number }

POST /search
  Body: { query: string, user_id: string, limit: number }
  Return: { results: [{ recording_id, chunk_text, score }] }

POST /quiz/generate
  Body: { transcript_id: string, count: number }
  Return: { quizzes: [{ question, options, answer }] }
```

---




## 9. 환경 변수 목록

```bash
# .env (서버)
OPENAI_API_KEY=sk-...
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJ...

# .env (모바일 - Expo)
EXPO_PUBLIC_SUPABASE_URL=https://xxx.supabase.co
EXPO_PUBLIC_SUPABASE_ANON_KEY=eyJ...
EXPO_PUBLIC_API_URL=https://your-server.render.com
```

---

## 10. 초기 세팅 커맨드

```bash
# 1. Expo 앱 초기화
npx create-expo-app@latest recordoc --template blank-typescript
cd voicedoc/apps/mobile
npx expo install expo-av expo-file-system @supabase/supabase-js

# 2. EAS CLI 설치 및 설정
npm install -g eas-cli
eas login
eas build:configure

# 3. FastAPI 서버 초기화
mkdir server && cd server
python -m venv venv && source venv/bin/activate
pip install fastapi uvicorn openai langchain langchain-openai supabase python-multipart

# 4. Supabase CLI (마이그레이션 관리)
npm install -g supabase
supabase init
supabase db push
```



*이 문서는 Claude와 함께 작성되었습니다. Codex 또는 다른 AI 코딩 어시스턴트에서 프로젝트 컨텍스트로 사용하세요.*
