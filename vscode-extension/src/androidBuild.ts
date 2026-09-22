/**
 * Resuelve los pedidos pendientes de android_build_and_screenshot (ver
 * tool_integration/adapters/vscode_android.py): compila el proyecto
 * Android real del workspace con Gradle, lo instala en un dispositivo
 * conectado (USB o WiFi, vía adb) y muestra una captura de pantalla
 * real — para que el usuario pueda monitorear visualmente el progreso
 * de una app mientras se construye (pedido explícito del usuario,
 * 2026-07-30).
 *
 * Corre ENTERAMENTE del lado de esta extensión, como procesos nativos
 * (gradlew/adb) — fuera del sandbox Docker de las Skills (que no tiene
 * acceso a USB/red de propósito general), mismo criterio ya establecido
 * para propose_project_files/import_resource: el backend de Python no
 * conoce el workspace real ni tiene acceso a ningún dispositivo, así
 * que esto no podría sandboxearse del mismo modo aunque quisiéramos.
 *
 * A diferencia de read_workspace_file, esto NO se encadena de vuelta a
 * otro paso del agente — el modelo no necesita "ver" los píxeles de la
 * captura para dar una respuesta razonable (ya la dio: "estoy
 * compilando, en un momento vas a ver el resultado"). El resultado se
 * le muestra al usuario DIRECTAMENTE en el panel de chat.
 *
 * Parte de la API real de vscode + child_process, no verificable en
 * este entorno sin un VS Code real corriendo — el parseo/formato puro
 * (sin ninguna de esas dependencias) vive en androidBuildFormat.ts,
 * testeable con Node normal.
 */
import { spawn } from "child_process";
import * as path from "path";
import * as vscode from "vscode";
import { ChatResult, KalClient } from "./kalClient";
import { findAndroidBuildRequestArtifact, parseAdbDevices, truncateProcessOutput } from "./androidBuildFormat";

// "Ya se mostró el aviso de seguridad de android_build_and_screenshot
// al menos una vez" — pedido explícito del usuario (2026-07-30): un
// aviso puntual, no repetido cada vez. globalState (no un campo de
// instancia) porque tiene que sobrevivir a cerrar y reabrir VS Code —
// "una sola vez" en el sentido literal, no "una vez por sesión".
const _SECURITY_NOTICE_SHOWN_KEY = "kal.androidBuild.securityNoticeShown";

function runProcess(command: string, args: string[], cwd?: string): Promise<{ code: number | null; output: string }> {
  return new Promise((resolve) => {
    const child = spawn(command, args, { cwd, shell: process.platform === "win32" });
    let output = "";
    child.stdout?.on("data", (chunk) => {
      output += chunk.toString();
    });
    child.stderr?.on("data", (chunk) => {
      output += chunk.toString();
    });
    child.on("error", (err) => {
      resolve({ code: null, output: `${output}\n${err.message}` });
    });
    child.on("close", (code) => {
      resolve({ code, output });
    });
  });
}

/** stdout crudo, en bytes — para screencap (imagen binaria, nunca texto). */
function runProcessBinary(command: string, args: string[]): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args);
    const chunks: Buffer[] = [];
    child.stdout.on("data", (chunk) => chunks.push(chunk));
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`'${command} ${args.join(" ")}' terminó con código ${code}`));
        return;
      }
      resolve(Buffer.concat(chunks));
    });
  });
}

/** Busca la raíz del proyecto Android: la carpeta que contiene el wrapper de Gradle (gradlew/gradlew.bat). */
async function findAndroidProjectRoot(): Promise<string | undefined> {
  const wrapperPattern = process.platform === "win32" ? "**/gradlew.bat" : "**/gradlew";
  const matches = await vscode.workspace.findFiles(wrapperPattern, "**/node_modules/**", 5);
  if (matches.length === 0) {
    return undefined;
  }
  // Si hay más de un proyecto Android en el workspace, se usa el
  // primero (orden de vscode.workspace.findFiles) — caso raro, no
  // resuelto acá (mismo criterio de alcance acotado que el resto del
  // diseño, ver docs/HISTORY.md).
  return path.dirname(matches[0].fsPath);
}

async function findBuiltApk(projectRoot: string): Promise<string | undefined> {
  const matches = await vscode.workspace.findFiles(
    new vscode.RelativePattern(projectRoot, "**/outputs/apk/debug/*.apk"),
    undefined,
    1
  );
  return matches.length > 0 ? matches[0].fsPath : undefined;
}

async function extractPackageName(apkPath: string): Promise<string | undefined> {
  // aapt (Android SDK build-tools) lee el manifiesto YA COMPILADO del
  // APK — más confiable que parsear AndroidManifest.xml/build.gradle a
  // mano, porque el applicationId real puede definirse en cualquiera
  // de los dos según la versión del Android Gradle Plugin.
  const { code, output } = await runProcess("aapt", ["dump", "badging", apkPath]);
  if (code !== 0) {
    return undefined;
  }
  const match = output.match(/package: name='([^']+)'/);
  return match ? match[1] : undefined;
}

async function ensureSecurityNoticeShownOnce(context: vscode.ExtensionContext): Promise<void> {
  if (context.globalState.get<boolean>(_SECURITY_NOTICE_SHOWN_KEY, false)) {
    return;
  }
  // No modal, no bloquea el flujo — pedido explícito del usuario: un
  // aviso puntual informativo, no una fricción que haya que confirmar
  // cada vez (ver docs/HISTORY.md para la discusión completa de por
  // qué esto NO es un bloqueo estricto).
  vscode.window.showWarningMessage(
    "Kal-in va a compilar tu proyecto Android e instalarlo en el dispositivo conectado. Compilar con Gradle " +
      "ejecuta código real (mismo riesgo que cualquier build de Gradle, con o sin kal-in) — si prefieres " +
      "minimizarlo, puedes desconectar el dispositivo hasta que termine de compilar. Este aviso no se repite."
  );
  await context.globalState.update(_SECURITY_NOTICE_SHOWN_KEY, true);
}

/**
 * Si `result` trae un pedido pendiente de android_build_and_screenshot,
 * lo resuelve: detecta el proyecto, compila, instala, lanza y captura
 * — mostrando el resultado real (éxito con captura, o el error real)
 * directamente en el chat vía `postToChat`. Nunca encadena de vuelta a
 * /chat (a diferencia de read_workspace_file) — el modelo ya dio su
 * respuesta final, esto es trabajo asincrónico que el usuario ve
 * directamente.
 */
export async function maybeHandleAndroidBuild(
  result: ChatResult,
  client: KalClient,
  postToChat: (message: unknown) => void,
  context: vscode.ExtensionContext
): Promise<void> {
  const artifact = findAndroidBuildRequestArtifact(result);
  if (!artifact) {
    return;
  }

  const projectRoot = await findAndroidProjectRoot();
  if (!projectRoot) {
    postToChat({
      type: "project-files-notice",
      text: "⚠️ No encontré ningún proyecto Android real en el workspace (falta el wrapper 'gradlew') — no se compiló nada.",
    });
    await client.reportAndroidBuildOutcome(artifact.request_id, "build_failed", "no se encontró gradlew en el workspace");
    return;
  }

  const devicesResult = await runProcess("adb", ["devices", "-l"]);
  const devices = parseAdbDevices(devicesResult.output).filter((d) => d.state === "device");
  if (devices.length === 0) {
    postToChat({
      type: "project-files-notice",
      text:
        "⚠️ No hay ningún dispositivo Android conectado — conecta el teléfono por USB (y acepta el diálogo " +
        "de depuración USB que aparece en la pantalla), o emparéjalo por WiFi desde Opciones de " +
        "desarrollador → Depuración inalámbrica. No se compiló nada.",
    });
    await client.reportAndroidBuildOutcome(artifact.request_id, "no_device");
    return;
  }
  if (devices.length > 1) {
    postToChat({
      type: "project-files-notice",
      text: `⚠️ Hay ${devices.length} dispositivos conectados a la vez — desconecta todos menos uno e intenta de nuevo.`,
    });
    await client.reportAndroidBuildOutcome(artifact.request_id, "no_device", `${devices.length} dispositivos conectados`);
    return;
  }
  const targetSerial = devices[0].serial;

  await ensureSecurityNoticeShownOnce(context);

  postToChat({ type: "project-files-notice", text: "🔨 Compilando el proyecto con Gradle — puede tardar unos minutos..." });

  const gradlewScript = process.platform === "win32" ? "gradlew.bat" : "./gradlew";
  const buildResult = await runProcess(gradlewScript, ["assembleDebug"], projectRoot);
  if (buildResult.code !== 0) {
    postToChat({
      type: "project-files-notice",
      text: `❌ La compilación falló:\n${truncateProcessOutput(buildResult.output)}`,
    });
    await client.reportAndroidBuildOutcome(artifact.request_id, "build_failed", truncateProcessOutput(buildResult.output));
    return;
  }

  const apkPath = await findBuiltApk(projectRoot);
  if (!apkPath) {
    postToChat({
      type: "project-files-notice",
      text: "❌ La compilación terminó bien pero no encontré el APK generado — revisa el proyecto manualmente.",
    });
    await client.reportAndroidBuildOutcome(artifact.request_id, "build_failed", "compilación exitosa pero APK no encontrado");
    return;
  }

  const installResult = await runProcess("adb", ["-s", targetSerial, "install", "-r", apkPath]);
  if (installResult.code !== 0) {
    postToChat({
      type: "project-files-notice",
      text: `❌ No se pudo instalar el APK en el dispositivo:\n${truncateProcessOutput(installResult.output)}`,
    });
    await client.reportAndroidBuildOutcome(artifact.request_id, "build_failed", truncateProcessOutput(installResult.output));
    return;
  }

  const packageName = await extractPackageName(apkPath);
  if (packageName) {
    // monkey con category LAUNCHER: lanza la actividad principal sin
    // necesitar saber su nombre de clase exacto (a diferencia de `adb
    // shell am start -n <package>/<activity>`).
    await runProcess("adb", ["-s", targetSerial, "shell", "monkey", "-p", packageName, "-c", "android.intent.category.LAUNCHER", "1"]);
    // Margen para que la app real termine de renderizar antes de
    // capturar — sin esto, la captura puede salir en negro/splash.
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }

  let screenshotBase64: string;
  try {
    const screenshotBytes = await runProcessBinary("adb", ["-s", targetSerial, "exec-out", "screencap", "-p"]);
    screenshotBase64 = screenshotBytes.toString("base64");
  } catch (e) {
    postToChat({
      type: "project-files-notice",
      text: `⚠️ Se instaló la app, pero no se pudo capturar la pantalla: ${e instanceof Error ? e.message : e}`,
    });
    await client.reportAndroidBuildOutcome(artifact.request_id, "installed", "instalado, captura falló");
    return;
  }

  postToChat({
    type: "android-build-notice",
    text: "✅ Compilado e instalado — así se ve ahora:",
    imageDataUri: `data:image/png;base64,${screenshotBase64}`,
  });
  await client.reportAndroidBuildOutcome(artifact.request_id, "installed", `instalado en ${targetSerial}`);
}
