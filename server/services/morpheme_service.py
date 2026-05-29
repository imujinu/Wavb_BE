

import logging

# MeCab 임포트 실패 시 None으로 유지 — 런타임 에러가 아닌 graceful fallback 처리
try:
    import MeCab as _mecab_module
except ImportError:
    _mecab_module = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# FTS에서 의미 있는 검색어로 활용 가능한 품사 접두사 집합.
# NN* (일반명사/고유명사/의존명사), VV (동사), VA (형용사 어간), MA (부사)
_VALID_POS_PREFIXES = ("NN", "VV", "VA", "MA")


class MorphemeService:


    def __init__(self) -> None:
        # MeCab 인스턴스 생성. 라이브러리가 없거나 사전 경로 오류 시 fallback 모드로 설정
        if _mecab_module is None:
            logger.warning(
                "MeCab 라이브러리를 찾을 수 없습니다. "
                "tokenize()는 원문 텍스트를 그대로 반환합니다."
            )
            self._tagger = None
        else:
            try:
                self._tagger = _mecab_module.Tagger()
            except RuntimeError:
                # MeCab 사전 경로 설정 오류 등 초기화 실패 시 fallback
                logger.warning(
                    "MeCab.Tagger 초기화에 실패했습니다. "
                    "tokenize()는 원문 텍스트를 그대로 반환합니다."
                )
                self._tagger = None

    def tokenize(self, text: str) -> str:
       
        # 1. fallback 모드 — MeCab 사용 불가 시 원문 반환
        if self._tagger is None:
            return text

        # 2. MeCab 파싱 수행
        tokens: list[str] = []
        node = self._tagger.parseToNode(text)

        # 3. 노드 순회하며 품사 필터링
        while node:
            # node.surface: 표층형 (실제 텍스트), node.feature: 품사 등 분석 정보가 쉼표로 구분된 문자열
            surface = node.surface
            feature = node.feature

            # BOS/EOS 노드(surface가 빈 문자열)는 건너뜀
            if surface and feature:
                # 4. feature 첫 번째 필드가 품사 — 예: "NNG,일반,...", "VV,..."
                pos = feature.split(",")[0]
                if pos.startswith(_VALID_POS_PREFIXES):
                    tokens.append(surface)

            node = node.next

        # 5. 유효 토큰이 없으면 원문 fallback (의미 있는 결과 보장)
        if not tokens:
            return text

        return " ".join(tokens)
