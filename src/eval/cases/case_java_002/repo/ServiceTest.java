public class ServiceTest {
    public static void main(String[] args) {
        Service s = new Service();
        assert s.process() != null;
        System.out.println("PASS");
    }
}
