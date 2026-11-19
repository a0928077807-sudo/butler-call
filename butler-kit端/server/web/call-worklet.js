// 網頁版通話的收音處理器（2026-11-18）：在音訊執行緒把麥克風的 Float32 原樣丟回主執行緒，
// 降頻、切包、送出都在 call.js 做（那邊才拿得到 WebSocket）。
// 用 AudioWorklet 不用 ScriptProcessorNode：後者已經被標成棄用，而且跑在主執行緒，畫面一忙就掉音。
class CallMic extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("call-mic", CallMic);
