import { WSClient } from "../lib/ws-client.js";

const SERVER_URL = "ws://127.0.0.1:8765/ws";
let client = null;

function connectWS() {
  if (client) {
    client.disconnect();
  }

  client = new WSClient(
    SERVER_URL,
    9999, // shared/dummy tabId for handshake
    (wsMsg) => {
      chrome.runtime.sendMessage({
        source: "offscreen",
        type: "WS_MESSAGE",
        payload: wsMsg
      });
    },
    () => {
      chrome.runtime.sendMessage({
        source: "offscreen",
        type: "WS_CLOSE"
      });
    },
    () => {
      chrome.runtime.sendMessage({
        source: "offscreen",
        type: "WS_OPEN"
      });
    }
  );

  client.connect();
}

// Initialize connection
connectWS();

chrome.runtime.onMessage.addListener((message) => {
  if (message.target === "offscreen") {
    if (message.type === "SEND_WS") {
      if (client) {
        client.send(message.payload);
      }
    } else if (message.type === "RECONNECT") {
      connectWS();
    }
  }
  return false; // No async response needed
});
