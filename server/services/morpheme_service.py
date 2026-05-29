

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
_VALID_POS_PREFIXES = ("NN", "NR", "NP", "VV", "VA", "MA")


class MorphemeService:
   

    def __init__(self) -> None:
        # Kiwi 인스턴스 생성. 라이브러리가 없거나 초기화 오류 시 fallback 모드로 설정
        if _Kiwi is None:
            logger.warning(
                "kiwipiepy 라이브러리를 찾을 수 없습니다. "
                "tokenize()는 원문 텍스트를 그대로 반환합니다."
            )
            self._kiwi = None
        else:
            try:
                self._kiwi = _Kiwi()
            except Exception:
                # 모델 로딩 실패 등 초기화 오류 시 fallback
                logger.warning(
                    "Kiwi 인스턴스 초기화에 실패했습니다. "
                    "tokenize()는 원문 텍스트를 그대로 반환합니다."
                )
                self._kiwi = None

    def tokenize(self, text: str) -> str:
     
        # 1. fallback 모드 — Kiwi 사용 불가 시 원문 반환
        if self._kiwi is None:
            return text

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
        except Exception:
            # 예기치 못한 분석 오류 시 원문 반환
            return text

        # 4. 유효 토큰이 없으면 원문 fallback (의미 있는 결과 보장)
        if not tokens:
            return text

        return " ".join(tokens)
