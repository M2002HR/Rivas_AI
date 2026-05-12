from rivas.mira_service import MiraServiceAPI, MiraServiceClient


def test_submodule_exports():
    assert MiraServiceAPI is not None
    assert MiraServiceClient is not None
