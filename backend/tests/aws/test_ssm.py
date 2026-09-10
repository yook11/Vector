"""SSM取得の復号・通信設定・秘密情報と終了の境界を検証する。"""

from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from app.aws import ssm


@pytest.fixture
def sdk(monkeypatch):
    client = Mock()
    client.get_parameter.return_value = {"Parameter": {"Value": "private-key"}}
    session = Mock()
    session.create_client.return_value = client
    monkeypatch.setattr(ssm, "Session", Mock(return_value=session))
    return session, client


def test_get_secret_and_config(sdk):
    session, client = sdk
    path = "/vector/embedding-consumer/gemini-api-key"
    result = ssm.get_secret_parameter(region="ap-northeast-1", path=path)
    assert isinstance(result, SecretStr)
    assert result.get_secret_value() == "private-key"
    assert "private-key" not in repr(result)
    client.get_parameter.assert_called_once_with(Name=path, WithDecryption=True)
    config = session.create_client.call_args.kwargs["config"]
    assert config.connect_timeout == 3 and config.read_timeout == 5
    assert config.retries == {"mode": "standard", "total_max_attempts": 2}
    assert config.proxies == {} and config.ignore_configured_endpoint_urls
    client.close.assert_called_once()


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"Parameter": None},
        {"Parameter": {}},
        {"Parameter": {"Value": None}},
        {"Parameter": {"Value": 1}},
        {"Parameter": {"Value": ""}},
        {"Parameter": {"Value": " \n"}},
    ],
)
def test_invalid_secret(sdk, response):
    sdk[1].get_parameter.return_value = response
    with pytest.raises(
        ValueError, match="SSM secret parameter must contain a non-empty string"
    ):
        ssm.get_secret_parameter(region="ap-northeast-1", path="/key")
    sdk[1].close.assert_called_once()


@pytest.mark.parametrize("failure", [None, RuntimeError("original")])
@pytest.mark.parametrize("log_fails", [False, True])
def test_cleanup_preserves_result(sdk, monkeypatch, failure, log_fails):
    sdk[1].close.side_effect = RuntimeError("private-key")
    log = Mock(side_effect=RuntimeError("log") if log_fails else None)
    monkeypatch.setattr(ssm.logger, "warning", log)
    if failure:
        sdk[1].get_parameter.side_effect = failure
        with pytest.raises(RuntimeError) as caught:
            ssm.get_secret_parameter(region="ap-northeast-1", path="/key")
        assert caught.value is failure
    else:
        assert (
            ssm.get_secret_parameter(
                region="ap-northeast-1", path="/key"
            ).get_secret_value()
            == "private-key"
        )
    assert "private-key" not in repr(log.call_args_list)


@pytest.mark.parametrize("status,expected_attempts", [(503, 2), (400, 1)])
def test_real_sdk_retry_limit(monkeypatch, status, expected_attempts):
    import json
    from types import SimpleNamespace

    from botocore.awsrequest import AWSResponse
    from botocore.exceptions import ClientError
    from botocore.session import Session

    session = Session()
    session.set_credentials("testing", "testing")
    create = session.create_client
    attempts = []

    def send(request):
        attempts.append(request)
        code = status
        body = {"__type": "ParameterNotFound", "message": "missing"}
        if status == 503:
            body = {"__type": "InternalServerError", "message": "temporary"}
            if len(attempts) == 2:
                code = 200
                body = {"Parameter": {"Value": "private"}}
        data = json.dumps(body).encode()
        return AWSResponse(
            request.url, code, {}, SimpleNamespace(stream=lambda: [data])
        )

    def make(*args, **kwargs):
        client = create(*args, **kwargs)
        monkeypatch.setattr(client._endpoint.http_session, "send", send)
        return client

    monkeypatch.setattr(session, "create_client", make)
    monkeypatch.setattr(ssm, "Session", lambda: session)
    if status == 503:
        assert (
            ssm.get_secret_parameter(
                region="ap-northeast-1", path="/key"
            ).get_secret_value()
            == "private"
        )
    else:
        with pytest.raises(ClientError):
            ssm.get_secret_parameter(region="ap-northeast-1", path="/key")
    assert len(attempts) == expected_attempts
