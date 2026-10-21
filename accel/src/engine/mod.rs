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

//! Native fill engine, session state, bar loop, and trace/report assembly.
//!
//! The implementation is confined to this module and implements the stable
//! [`SessionEngine`](crate::types::SessionEngine) contract directly. Strategy
//! evaluation remains behind [`NativeStrategy`](crate::types::NativeStrategy);
//! no Python callback or per-bar FFI crossing is introduced here.

#![allow(dead_code)]

mod fill;
mod loop_driver;
mod report;

#[allow(unused_imports)]
pub(crate) use fill::FillEngine;
#[allow(unused_imports)]
pub(crate) use loop_driver::{
    run_configured, run_configured_window, run_configured_window_with_trace,
    run_configured_window_with_trace_sink, run_configured_with_trace,
    run_configured_with_trace_sink, run_native, run_native_window, run_native_window_with_trace,
    run_native_window_with_trace_sink, run_native_with_trace, run_native_with_trace_sink,
};

#[cfg(test)]
mod tests;
