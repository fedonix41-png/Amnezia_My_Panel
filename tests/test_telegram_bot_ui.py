import pytest
import telegram_bot
from telegram_bot import _format_smart_config_card, TelegramAPI


def test_format_smart_config_card_wireguard():
    conn_name = "My WG"
    server_name = "Frankfurt-1"
    proto = "wireguard"
    config = "[Interface]\nPrivateKey = test_key\nAddress = 10.0.0.2/32\n\n[Peer]\nPublicKey = peer_key\nEndpoint = 1.2.3.4:51820"
    
    text, reply_markup = _format_smart_config_card(
        conn_name=conn_name,
        server_name=server_name,
        proto=proto,
        config=config,
        conn_id="conn123",
        generate_vpn_link_fn=lambda cfg: "vpn://link"
    )
    
    assert "✅ <b>My WG</b>" in text
    assert "🌐 Server: <b>Frankfurt-1</b>" in text
    assert "🔌 Protocol: <b>WIREGUARD</b>" in text
    assert "🔗 <b>VPN Link</b>" in text
    assert "<blockquote expandable><code>vpn://link</code></blockquote>" in text
    assert "<blockquote expandable><code>[Interface]" in text
    
    kb = reply_markup.get("inline_keyboard", [])
    assert len(kb) == 2
    assert kb[0][0]["text"] == "📱 QR-код"
    assert kb[0][0]["callback_data"] == "qr:conn123"
    assert kb[0][1]["text"] == "📁 Скачать .conf"
    assert kb[0][1]["callback_data"] == "dl:conn123"
    assert kb[1][0]["text"] == "🔙 К подключениям"
    assert kb[1][0]["callback_data"] == "refresh"


def test_format_smart_config_card_link_proto():
    conn_name = "My VLESS"
    server_name = "Amsterdam-2"
    proto = "xray"
    config = "vless://uuid@1.2.3.4:443?security=reality"
    
    text, reply_markup = _format_smart_config_card(
        conn_name=conn_name,
        server_name=server_name,
        proto=proto,
        config=config,
        conn_id="conn456"
    )
    
    assert "✅ <b>My VLESS</b>" in text
    assert "🔗 <b>Connection link</b>" in text
    assert "<blockquote expandable><code>vless://uuid@1.2.3.4:443?security=reality</code></blockquote>" in text

    
    kb = reply_markup.get("inline_keyboard", [])
    assert len(kb) == 2
    assert kb[0][0]["text"] == "📱 QR-код"
    assert kb[0][0]["callback_data"] == "qr:conn456"
    # Link proto should not offer .conf download
    assert len(kb[0]) == 1
