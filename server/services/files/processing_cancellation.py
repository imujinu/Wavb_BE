from collections.abc import Awaitable, Callable


CancellationChecker = Callable[[], Awaitable[bool]]


class ProcessingCancelledError(Exception):
    """사용자가 요청한 처리 중단을 내부 파이프라인에 전달하는 예외."""


async def raise_if_cancel_requested(
    cancellation_checker: CancellationChecker | None,
) -> None:
    """
    기능 요약: 선택적으로 전달된 취소 확인 함수를 호출해 중단 요청이 있으면 예외를 발생시킨다.
    기능 흐름:
        1. cancellation_checker가 없으면 아무 작업 없이 반환한다.
        2. checker가 True를 반환하면 ProcessingCancelledError를 발생시켜 상위 단계가 저장/호출을 멈추게 한다.
    파라미터:
        cancellation_checker: 현재 작업의 취소 요청 여부를 비동기로 확인하는 함수.
    """
    if cancellation_checker is not None and await cancellation_checker():
        raise ProcessingCancelledError("Processing was cancelled by user.")
