import { Component, type ReactNode } from "react";

export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex items-center justify-between rounded-sm border border-red-950 bg-red-950/20 px-4 py-3 font-mono text-[11px] text-red-400">
          <span>renderer error: {this.state.error.message}</span>
          <button onClick={() => this.setState({ error: null })}
            className="border border-red-900 px-2 py-0.5 text-zinc-300 hover:text-zinc-100">
            reload panel
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
