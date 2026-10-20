// Copyright 2026 Joshua Hudson
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

//! Load and inspect a v2 `program_ir` payload (`pl_transpiler.ir`).

use crate::types::KernelError;
use serde_json::Value;

/// Schema version written by `pl_transpiler.ir.IR_SCHEMA_VERSION`.
pub(crate) const IR_SCHEMA_VERSION: i64 = 1;
pub(crate) const IR_KIND: &str = "pl_transpiler.ast";

/// Decode a `serialise_ir` payload and return the program AST object.
///
/// Unknown `ir_version` / `kind` / a non-program root are refused loudly —
/// never guessed (v2 requirement 6).
pub(crate) fn load_program_ir(payload: &str) -> Result<Value, KernelError> {
    let data: Value = serde_json::from_str(payload)
        .map_err(|error| KernelError::input(format!("malformed program_ir JSON: {error}")))?;
    let obj = data
        .as_object()
        .ok_or_else(|| KernelError::input("malformed program_ir: expected a JSON object"))?;
    let kind = obj.get("kind").and_then(Value::as_str).unwrap_or("");
    if kind != IR_KIND {
        return Err(KernelError::unsupported(format!(
            "program_ir kind {kind:?} is not {IR_KIND:?}"
        )));
    }
    let version = obj.get("ir_version").and_then(Value::as_i64);
    if version != Some(IR_SCHEMA_VERSION) {
        return Err(KernelError::unsupported(format!(
            "unsupported IR schema version {version:?}; this reader supports {IR_SCHEMA_VERSION}"
        )));
    }
    let ast = obj
        .get("ast")
        .cloned()
        .ok_or_else(|| KernelError::input("malformed program_ir: missing 'ast'"))?;
    if node_type(&ast) != Some("program") {
        return Err(KernelError::input(format!(
            "IR root must be a 'program' node, got {:?}",
            node_type(&ast)
        )));
    }
    Ok(ast)
}

pub(crate) fn node_type(node: &Value) -> Option<&str> {
    node.get("type").and_then(Value::as_str)
}

pub(crate) fn ast_uses_data2(ast: &Value) -> bool {
    // A program needs a second bar stream only when a `data_of` node names a
    // data series *other than* Data1 (`Close of Data2` → `data_num == 2`). A
    // `Close of Data1` node (`data_num == 1`) is just the primary series and
    // resolves to Data1, so it does not demand a `data2` tape.
    uses_data_stream(ast, 2)
}

fn uses_data_stream(node: &Value, min_data_num: i64) -> bool {
    match node {
        Value::Object(map) => {
            if map.get("type").and_then(Value::as_str) == Some("data_of") {
                if let Some(data_num) = map.get("data_num").and_then(Value::as_i64) {
                    if data_num >= min_data_num {
                        return true;
                    }
                }
            }
            map.values()
                .any(|child| uses_data_stream(child, min_data_num))
        }
        Value::Array(items) => items
            .iter()
            .any(|child| uses_data_stream(child, min_data_num)),
        _ => false,
    }
}
