public class CalculatorTest {
    public static void main(String[] args) {
        Calculator c = new Calculator();
        assert c.getValue() == 42 : "Expected 42";
        System.out.println("PASS");
    }
}
