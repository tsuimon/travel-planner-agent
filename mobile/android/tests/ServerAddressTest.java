import cn.travelplanner.mobile.ServerAddress;
import java.net.URI;

/** JVM regression tests; no Android device or network connection required. */
public class ServerAddressTest {
    private static int checks;
    private static void check(boolean value) {
        if (!value) throw new AssertionError("Failed check " + checks);
        checks++;
    }
    private static void reject(String value) {
        try {
            ServerAddress.parse(value);
            throw new AssertionError("Unexpected address accepted: " + value);
        } catch (IllegalArgumentException expected) { checks++; }
    }
    public static void main(String[] args) {
        check(ServerAddress.parse("192.168.1.8:8001/ui/").toString().equals("http://192.168.1.8:8001"));
        check(ServerAddress.parse("http://10.66.137.70:8001").getHost().equals("10.66.137.70"));
        check(ServerAddress.parse("https://planner.example.com/").toString().equals("https://planner.example.com"));
        reject("http://127.0.0.1:8000");
        reject("http://localhost:8000");
        reject("http://[::1]:8000");
        reject("http://8.8.8.8");
        reject("http://192.168.1.8.attacker.example");
        reject("http://user:password@192.168.1.8");
        reject("https://example.com/?token=secret");
        reject("https://example.com/#token=secret");
        reject("file:///etc/passwd");
        reject("javascript:alert(1)");
        reject("http://192.168.1.8:65536");
        reject("http://192.168.999.1");
        URI base = ServerAddress.parse("https://planner.example.com");
        check(ServerAddress.sameOrigin(base, URI.create("https://planner.example.com:443/ui/")));
        check(!ServerAddress.sameOrigin(base, URI.create("http://planner.example.com/ui/")));
        check(!ServerAddress.sameOrigin(base, URI.create("https://planner.example.com.attacker.example/")));
        check(!ServerAddress.sameOrigin(base, URI.create("https://planner.example.com:8443/")));
        System.out.println("ServerAddress: " + checks + " checks passed.");
    }
}
