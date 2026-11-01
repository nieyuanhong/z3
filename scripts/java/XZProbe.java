// Fork-only measurement using the actual NativeLibraryLoader and real JNI.
import com.microsoft.z3.Context;
import com.microsoft.z3.IntExpr;
import com.microsoft.z3.Solver;
import com.microsoft.z3.Status;
import com.microsoft.z3.Version;

public final class XZProbe {
    public static void main(String[] args) throws Exception {
        long start = System.nanoTime();
        Class.forName("com.microsoft.z3.Native");
        double load = (System.nanoTime() - start) / 1000000.0;
        try (Context context = new Context()) {
            Solver solver = context.mkSolver();
            IntExpr x = context.mkIntConst("x");
            solver.add(context.mkEq(x, context.mkInt(42)));
            if (solver.check() != Status.SATISFIABLE) { throw new AssertionError("SAT"); }
            solver.add(context.mkEq(x, context.mkInt(43)));
            if (solver.check() != Status.UNSATISFIABLE) { throw new AssertionError("UNSAT"); }
        }
        System.out.println("PROBE {\"load_ms\":" + load + ",\"version\":\"" + Version.getString()
            + "\",\"java_spec\":\"" + System.getProperty("java.specification.version") + "\"}");
    }
}
