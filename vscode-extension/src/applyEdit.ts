/**
 * Orquesta "Kal: Aplicar cambios a la selección" — parte que sí toca la
 * API real de vscode (editor activo, diff nativo, WorkspaceEdit), no
 * verificable en este entorno sin un VS Code real corriendo. La lógica
 * de armado del prompt y extracción del código vive en
 * applyEditFormat.ts (sin import de vscode, testeable con Node normal).
 */
import * as vscode from "vscode";
import { buildEditGoal, checkBraceBalance, extractCodeBlock } from "./applyEditFormat";
import { captureEditorSnapshot } from "./editorContext";
import { KalClient } from "./kalClient";

function fullDocumentRange(document: vscode.TextDocument): vscode.Range {
  const lastLine = document.lineAt(document.lineCount - 1);
  return new vscode.Range(0, 0, lastLine.lineNumber, lastLine.text.length);
}

export async function runApplySuggestedEdit(client: KalClient): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  const snapshot = captureEditorSnapshot();
  if (!editor || !snapshot) {
    vscode.window.showWarningMessage("Kal-in: no hay ningún editor activo para tomar contexto.");
    return;
  }

  // Bug real encontrado en uso: una selección que abre llaves/paréntesis
  // sin cerrarlos dentro de sí misma (p.ej. una clase cortada antes de
  // sus llaves de cierre, que están mucho más abajo en el archivo) hace
  // que kal complete el fragmento con sus propias llaves — que chocan
  // con las reales del archivo y lo rompen al aplicar. Solo aplica a
  // selecciones parciales: el archivo completo siempre es autocontenido.
  if (snapshot.isSelection) {
    const balance = checkBraceBalance(snapshot.text);
    if (!balance.isBalanced) {
      const proceed = await vscode.window.showWarningMessage(
        `La selección no parece autocontenida (${balance.detail}) — kal-in podría agregar llaves/paréntesis de cierre que choquen con el resto del archivo. ¿Seleccionaste un bloque completo (que se abre y cierra a sí mismo)?`,
        { modal: true },
        "Continuar de todos modos",
        "Cancelar"
      );
      if (proceed !== "Continuar de todos modos") {
        return;
      }
    }
  }

  const instruction = await vscode.window.showInputBox({
    prompt: "¿Qué cambio quieres aplicar?",
    placeHolder: "ej: agrega manejo de errores",
  });
  if (!instruction) {
    return;
  }

  const originalRange = snapshot.isSelection ? editor.selection : fullDocumentRange(editor.document);
  const goal = buildEditGoal(snapshot, instruction);

  const model = vscode.workspace.getConfiguration("kal").get<string>("model") || undefined;
  const finalAnswer = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "Kal-in está pensando el cambio..." },
    async () => {
      try {
        const result = await client.chat(goal, model);
        return result.final_answer;
      } catch (e) {
        vscode.window.showErrorMessage(String(e instanceof Error ? e.message : e));
        return null;
      }
    }
  );
  if (finalAnswer === null) {
    return;
  }

  const proposedCode = extractCodeBlock(finalAnswer);
  if (proposedCode === null) {
    const action = await vscode.window.showErrorMessage(
      "Kal-in no devolvió un bloque de código reconocible.",
      "Ver respuesta completa"
    );
    if (action === "Ver respuesta completa") {
      const doc = await vscode.workspace.openTextDocument({ content: finalAnswer });
      await vscode.window.showTextDocument(doc);
    }
    return;
  }

  const originalDoc = await vscode.workspace.openTextDocument({
    content: snapshot.text,
    language: snapshot.languageId,
  });
  const proposedDoc = await vscode.workspace.openTextDocument({
    content: proposedCode,
    language: snapshot.languageId,
  });
  await vscode.commands.executeCommand(
    "vscode.diff",
    originalDoc.uri,
    proposedDoc.uri,
    `Kal-in: cambio propuesto (${snapshot.relativePath})`
  );

  // modal: true a propósito — el mensaje sin modal (probado en uso real)
  // podía desaparecer solo (idle, o al interactuar con otra parte de VS
  // Code, p.ej. cerrar el panel de chat para hacer espacio en pantalla)
  // antes de que el usuario llegara a click Aplicar/Descartar, dejando
  // la decisión "perdida" sin que quede claro qué pasó. Modal se queda
  // fijo hasta una elección explícita (o Escape, que cuenta como
  // "Descartar" vía el chequeo de abajo).
  const choice = await vscode.window.showInformationMessage(
    `Kal-in propone un cambio para ${snapshot.relativePath}. ¿Aplicarlo?`,
    { modal: true },
    "Aplicar",
    "Descartar"
  );
  if (choice !== "Aplicar") {
    return;
  }

  const edit = new vscode.WorkspaceEdit();
  edit.replace(editor.document.uri, originalRange, proposedCode);
  await vscode.workspace.applyEdit(edit);
}
