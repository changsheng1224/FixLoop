public class AppTest {
    public static void main(String[] args) {
        App app = new App();
        Util util = new Util();
        assert app.run(util) == 20 : "Expected 20";
        System.out.println("PASS");
    }
}
