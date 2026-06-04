

import logging

# Kiwi 임포트 실패 시 None으로 유지 — Windows에서 MeCab 대신 순수 Python 라이브러리 사용
try:
    from kiwipiepy import Kiwi as _Kiwi
except ImportError:
    _Kiwi = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# FTS에서 의미 있는 검색어로 활용 가능한 품사 접두사/이름 집합.
# NN* (일반명사/고유명사/의존명사), NR (수사), NP (대명사),
# VV (동사 어간), VA (형용사 어간), MA* (부사)
# SL(외국어/영어), SN(숫자)을 포함해 "Transformer", "RAG", "GPT-4" 같은 혼합 강의 키워드를 보존한다.
_VALID_POS_PREFIXES = ("NN", "NR", "NP", "VV", "VA", "MA", "SL", "SN")


class MorphemeServiceError(RuntimeError):
    """형태소 분석을 수행할 수 없을 때 발생하는 에러."""


class MorphemeService:
   

    def __init__(self) -> None:
        # Kiwi 인스턴스 생성. 라이브러리가 없거나 초기화 오류가 나면 tokenize 단계에서 명시적으로 실패시킨다.
        self._init_error: str | None = None
        if _Kiwi is None:
            self._init_error = "kiwipiepy 라이브러리를 찾을 수 없습니다."
            logger.error(self._init_error)
            self._kiwi = None
        else:
            try:
                self._kiwi = _Kiwi()
            except Exception as exc:
                self._init_error = f"Kiwi 인스턴스 초기화에 실패했습니다: {exc}"
                logger.exception("Kiwi 인스턴스 초기화에 실패했습니다.")
                self._kiwi = None

    def tokenize(self, text: str) -> str:
     
        # 1. Kiwi 사용 불가 시 원문으로 대체하지 않고 명시적으로 실패시킨다.
        if self._kiwi is None:
            raise MorphemeServiceError(
                self._init_error or "Kiwi 형태소 분석기를 사용할 수 없습니다."
            )

        try:
            # 2. Kiwi 형태소 분석 수행 — result[0][0]이 최적 분석 결과 Token 리스트
            result = self._kiwi.analyze(text)
            tokens_list = result[0][0]

            # 3. 유효 품사 토큰 필터링
            #    token.form: 표층형(원형), token.tag.name: 품사 태그 문자열 (예: "NNG", "VV")
            # token.tag는 문자열 (예: "NNG", "VV") — enum이 아님에 주의
            tokens: list[str] = [
                token.form
                for token in tokens_list
                if token.tag.startswith(_VALID_POS_PREFIXES)
            ]
        except Exception as exc:
            # 예기치 못한 분석 오류 시 원문으로 대체하지 않고 호출자가 실패를 인지하게 한다.
            raise MorphemeServiceError("Kiwi 형태소 분석에 실패했습니다.") from exc

        # 4. 유효 토큰이 없으면 검색용 형태소 텍스트를 만들 수 없으므로 실패시킨다.
        if not tokens:
            raise MorphemeServiceError("검색 가능한 형태소 토큰이 없습니다.")

        return " ".join(tokens)
