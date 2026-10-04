
  window.__ds_is_benign_error = function (msg) {
    msg = String(msg || "");
    // 浏览器/组件库常见无害提示，不弹红条
    var ignore = [
      "ResizeObserver loop",
      "ResizeObserver loop completed with undelivered notifications",
      "Script error.",
      "ResizeObserver"
    ];
    for (var i = 0; i < ignore.length; i++) {
      if (msg.indexOf(ignore[i]) !== -1) return true;
    }
    return false;
  };
  window.__ds_show_error = function (msg) {
    try {
      if (window.__ds_is_benign_error(msg)) return;
      var el = document.getElementById("ds-boot-error");
      if (!el) {
        el = document.createElement("div");
        el.id = "ds-boot-error";
        el.style.cssText = "position:fixed;left:0;right:0;top:0;z-index:999999;background:#b91c1c;color:#fff;padding:14px 18px;font-size:14px;line-height:1.5;font-family:sans-serif;white-space:pre-wrap;";
        document.body.appendChild(el);
      }
      el.textContent = "页面加载出错：\n" + msg + "\n\n请尝试 Ctrl+F5 强制刷新，或清除本站 localStorage 后重试。";
    } catch (e) {}
  };
  window.addEventListener("error", function (e) {
    var msg = (e && e.message) || String(e);
    if (window.__ds_is_benign_error(msg)) {
      e.stopImmediatePropagation && e.stopImmediatePropagation();
      return;
    }
    window.__ds_show_error(msg);
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e && e.reason;
    var msg = (r && r.message) || String(r);
    if (window.__ds_is_benign_error(msg)) return;
    window.__ds_show_error(msg);
  });
