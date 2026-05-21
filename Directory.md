## 7. AI 기술 활용 상세

### 7-1. STT 파이프라인 (Whisper)

```python
# server/services/whisper_service.py
import openai

async def transcribe_audio(file_path: str) -> str:
    client = openai.AsyncOpenAI()
    with open(file_path, "rb") as audio_file:
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ko",         # 한국어 지정
            response_format="text"
        )
    return transcript
```

### 7-2. LangChain 요약 체인

```python
# server/services/langchain_service.py
from langchain_openai import ChatOpenAI
from langchain.prompts import ChatPromptTemplate
from langchain.chains import LLMChain

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3)

summary_prompt = ChatPromptTemplate.from_template("""
다음 음성 녹음의 스크립트를 요약해줘.
- 핵심 내용 3~5개 불릿포인트
- 전체 요약 2~3문장
- 주요 키워드 5개

스크립트:
{transcript}
""")

summary_chain = summary_prompt | llm
```

### 7-3. 문서 임베딩 + RAG 검색

```python
# server/services/vector_service.py
from langchain_openai import OpenAIEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter

embeddings_model = OpenAIEmbeddings(model="text-embedding-3-small")
splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)

async def embed_and_store(recording_id: str, transcript: str, supabase):
    chunks = splitter.split_text(transcript)
    for i, chunk in enumerate(chunks):
        vector = embeddings_model.embed_query(chunk)
        await supabase.table("embeddings").insert({
            "recording_id": recording_id,
            "chunk_text": chunk,
            "embedding": vector,
            "chunk_index": i
        }).execute()

async def similarity_search(query: str, user_id: str, supabase, limit=5):
    query_vector = embeddings_model.embed_query(query)
    # pgvector cosine similarity 검색
    result = await supabase.rpc("match_embeddings", {
        "query_embedding": query_vector,
        "user_id": user_id,
        "match_count": limit
    }).execute()
    return result.data
```

### 7-4. pgvector 검색 함수 (Supabase SQL)

```sql
CREATE OR REPLACE FUNCTION match_embeddings(
  query_embedding vector(1536),
  user_id UUID,
  match_count INT DEFAULT 5
)
RETURNS TABLE (
  recording_id UUID,
  chunk_text TEXT,
  score FLOAT
)
LANGUAGE SQL STABLE AS $$
  SELECT
    e.recording_id,
    e.chunk_text,
    1 - (e.embedding <=> query_embedding) AS score
  FROM embeddings e
  JOIN recordings r ON r.id = e.recording_id
  WHERE r.user_id = match_embeddings.user_id
  ORDER BY e.embedding <=> query_embedding
  LIMIT match_count;
$$;
```