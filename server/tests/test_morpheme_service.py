from dataclasses import dataclass

import pytest

from services.rag.morpheme_service import MorphemeService, MorphemeServiceError


@dataclass
class FakeToken:
    form: str
    tag: str


class FakeKiwi:
    def analyze(self, text: str):
        return [
            (
                [
                    FakeToken("RAG", "SL"),
                    FakeToken("GPT", "SL"),
                    FakeToken("4", "SN"),
                    FakeToken("강의", "NNG"),
                    FakeToken("은", "JX"),
                ],
                0.0,
            )
        ]


def test_tokenize_keeps_english_and_number_tokens() -> None:
    service = MorphemeService.__new__(MorphemeService)
    service._kiwi = FakeKiwi()

    assert service.tokenize("RAG GPT-4 강의는?") == "RAG GPT 4 강의"


def test_tokenize_raises_when_kiwi_is_unavailable() -> None:
    service = MorphemeService.__new__(MorphemeService)
    service._kiwi = None
    service._init_error = "kiwi unavailable"

    with pytest.raises(MorphemeServiceError, match="kiwi unavailable"):
        service.tokenize("강의 내용")


def test_tokenize_raises_when_no_searchable_tokens() -> None:
    class EmptyKiwi:
        def analyze(self, text: str):
            return [([FakeToken("?", "SF")], 0.0)]

    service = MorphemeService.__new__(MorphemeService)
    service._kiwi = EmptyKiwi()

    with pytest.raises(MorphemeServiceError, match="검색 가능한 형태소 토큰이 없습니다"):
        service.tokenize("???")
