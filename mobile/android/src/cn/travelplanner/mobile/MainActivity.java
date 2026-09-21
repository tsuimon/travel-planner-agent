package cn.travelplanner.mobile;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.view.View;
import android.view.inputmethod.InputMethodManager;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import java.net.URI;

/** Installable Android client for the existing planning service, Android 8+. */
public class MainActivity extends Activity {
    private LinearLayout root;
    private WebView web;
    private URI server;
    private ProgressBar progress;
    private TextView error;
    private boolean pageFailed;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        showSettings();
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    private void makeRoot() {
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(243, 248, 246));
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            v.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets;
        });
        setContentView(root);
        root.requestApplyInsets();
    }

    private TextView text(String content, int size) {
        TextView view = new TextView(this);
        view.setText(content);
        view.setTextSize(size);
        view.setTextColor(Color.rgb(26, 57, 50));
        view.setPadding(dp(4), dp(10), dp(4), dp(10));
        return view;
    }

    private void destroyWeb() {
        if (web != null) {
            web.stopLoading();
            web.destroy();
            web = null;
        }
    }

    private void showSettings() {
        destroyWeb();
        makeRoot();
        ScrollView scroll = new ScrollView(this);
        LinearLayout form = new LinearLayout(this);
        form.setOrientation(LinearLayout.VERTICAL);
        form.setPadding(dp(24), dp(24), dp(24), dp(24));
        form.addView(text("出行规划", 30));
        form.addView(text("自动安排路线与接驳", 18));
        form.addView(text("连接规划服务", 22));
        form.addView(text("首次使用：手机和电脑连接同一Wi-Fi，在电脑启动手机服务，把终端显示的地址填在下面。", 16));
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint("http://192.168.1.8:8001");
        input.setText(getPreferences(MODE_PRIVATE).getString("server", ""));
        input.setContentDescription("规划服务器地址");
        form.addView(input);
        Button connect = new Button(this);
        connect.setText("连接并开始规划");
        form.addView(connect);
        TextView hint = text("API密钥保留在电脑后端，无需在手机填写。电脑服务需要保持运行；出门使用时，请改为已部署的HTTPS服务地址。", 14);
        form.addView(hint);
        connect.setOnClickListener(v -> {
            try {
                server = ServerAddress.parse(input.getText().toString());
                getPreferences(MODE_PRIVATE).edit().putString("server", server.toString()).apply();
                ((InputMethodManager)getSystemService(INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(input.getWindowToken(), 0);
                showPlanner();
            } catch (IllegalArgumentException e) { input.setError(e.getMessage()); }
        });
        scroll.addView(form);
        root.addView(scroll);
    }

    private void showPlanner() {
        makeRoot();
        LinearLayout toolbar = new LinearLayout(this);
        Button settings = new Button(this);
        settings.setText("服务器");
        settings.setOnClickListener(v -> new AlertDialog.Builder(this).setMessage("返回服务器设置？当前页面将关闭。")
                .setNegativeButton("取消", null).setPositiveButton("返回", (dialog, which) -> showSettings()).show());
        toolbar.addView(settings);
        TextView title = text("出行规划", 18);
        toolbar.addView(title, new LinearLayout.LayoutParams(0, -2, 1));
        Button retry = new Button(this);
        retry.setText("重试");
        retry.setOnClickListener(v -> web.reload());
        toolbar.addView(retry);
        root.addView(toolbar);
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        root.addView(progress, new LinearLayout.LayoutParams(-1, dp(3)));
        error = text("", 15);
        error.setVisibility(View.GONE);
        root.addView(error);
        web = new WebView(this);
        WebSettings options = web.getSettings();
        options.setJavaScriptEnabled(true);
        options.setDomStorageEnabled(true);
        options.setAllowFileAccess(false);
        options.setAllowContentAccess(false);
        options.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        options.setSupportMultipleWindows(false);
        options.setJavaScriptCanOpenWindowsAutomatically(false);
        web.setWebChromeClient(new WebChromeClient() {
            @Override public void onProgressChanged(WebView view, int value) { progress.setProgress(value); }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
                pageFailed = false;
                error.setVisibility(View.GONE);
                progress.setVisibility(View.VISIBLE);
            }
            @Override public void onPageFinished(WebView view, String url) {
                progress.setVisibility(View.GONE);
                if (!pageFailed) error.setVisibility(View.GONE);
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                try {
                    URI target = URI.create(request.getUrl().toString());
                    if (ServerAddress.sameOrigin(server, target)) return false;
                    if (request.hasGesture() && ("https".equals(target.getScheme()) || "http".equals(target.getScheme()))) {
                        startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(target.toString())));
                    }
                } catch (Exception ignored) { showError("无法打开此链接。"); }
                return true;
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError failure) {
                if (request.isForMainFrame()) showError("连接失败。请确认电脑服务正在运行、手机和电脑在同一Wi-Fi，并检查服务器地址。恢复网络后点“重试”。");
            }
            @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (request.isForMainFrame()) showError("服务器返回HTTP " + response.getStatusCode() + "。请确认/ui/页面已启用；当前版本连接网页界面。");
            }
            @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError failure) {
                handler.cancel();
                showError("HTTPS证书无效，请修复服务器证书后重试。");
            }
        });
        root.addView(web, new LinearLayout.LayoutParams(-1, 0, 1));
        web.loadUrl(server.toString() + "/ui/");
    }

    private void showError(String message) {
        pageFailed = true;
        error.setText(message);
        error.setVisibility(View.VISIBLE);
        progress.setVisibility(View.GONE);
    }

    @Override public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else if (web != null) showSettings();
        else super.onBackPressed();
    }

    @Override protected void onDestroy() {
        destroyWeb();
        super.onDestroy();
    }
}
