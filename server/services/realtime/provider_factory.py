from services.realtime.stt_provider import STTProvider
from services.realtime.deepgram_provider import DeepgramProvider


def create_stt_provider() -> STTProvider:
    """
    STT provider 인스턴스를 생성합니다.

    factory 함수를 사용하는 이유:
    - WebSocket 세션마다 새 인스턴스가 필요합니다 (세션 상태 격리).
    - provider 타입을 런타임에 결정하므로 재배포 없이 전환 가능합니다.

    현재 지원: "deepgram" (기본값)
    추후 추가 시: settings.stt_provider 분기 확장
    """
    # 현재는 Deepgram만 지원. 추후 다른 provider 추가 시 분기 확장.
    return DeepgramProvider()
