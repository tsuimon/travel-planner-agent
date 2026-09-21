package cn.travelplanner.mobile;

import java.net.URI;
import java.util.Locale;

/** Restrict plaintext to explicitly entered LAN addresses; never embed provider keys. */
public final class ServerAddress {
    private ServerAddress() {}

    public static URI parse(String raw) {
        String value = raw.trim();
        if (!value.contains("://")) value = "http://" + value;
        URI uri;
        try { uri = new URI(value); }
        catch (Exception e) { throw new IllegalArgumentException("请输入完整的服务器地址。例：http://192.168.1.8:8001"); }
        String host = uri.getHost();
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        if (host == null || uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null
                || (uri.getPort() != -1 && (uri.getPort() < 1 || uri.getPort() > 65535))) {
            throw new IllegalArgumentException("地址不能包含账号、查询参数或无效端口。");
        }
        if (host.equalsIgnoreCase("localhost") || host.startsWith("127.") || host.equals("[::1]")) {
            throw new IllegalArgumentException("手机上的127.0.0.1指手机自己。请填电脑的局域网IP地址。");
        }
        if (!scheme.equals("https") && !(scheme.equals("http") && isPrivateIpv4(host))) {
            throw new IllegalArgumentException("HTTP仅用于同一Wi-Fi内的局域网IP；远程服务器请使用HTTPS。");
        }
        String path = uri.getPath();
        if (path != null && !path.isEmpty() && !path.equals("/") && !path.equals("/ui") && !path.equals("/ui/")) {
            throw new IllegalArgumentException("请填写服务器根地址，不要附加其他路径。");
        }
        return URI.create(scheme + "://" + uri.getRawAuthority());
    }

    private static boolean isPrivateIpv4(String host) {
        String[] parts = host.split("\\.", -1);
        if (parts.length != 4) return false;
        int[] numbers = new int[4];
        try {
            for (int i = 0; i < 4; i++) {
                if (!parts[i].matches("[0-9]{1,3}")) return false;
                numbers[i] = Integer.parseInt(parts[i]);
                if (numbers[i] > 255) return false;
            }
        } catch (NumberFormatException e) { return false; }
        return numbers[0] == 10 || (numbers[0] == 192 && numbers[1] == 168)
                || (numbers[0] == 172 && numbers[1] >= 16 && numbers[1] <= 31);
    }

    public static boolean sameOrigin(URI base, URI next) {
        if (next.getHost() == null || next.getScheme() == null || next.getUserInfo() != null) return false;
        int firstPort = base.getPort() == -1 ? (base.getScheme().equals("https") ? 443 : 80) : base.getPort();
        int secondPort = next.getPort() == -1 ? (next.getScheme().equals("https") ? 443 : 80) : next.getPort();
        return base.getScheme().equalsIgnoreCase(next.getScheme())
                && base.getHost().equalsIgnoreCase(next.getHost()) && firstPort == secondPort;
    }
}
