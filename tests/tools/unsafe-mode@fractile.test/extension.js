// TEST ONLY: see metadata.json. Lets the harness inspect the private
// headless shell (Eval, Screenshot). Never install in a real session.
export default class UnsafeMode {
    enable() {
        this._was = global.context.unsafe_mode;
        global.context.unsafe_mode = true;
    }

    disable() {
        global.context.unsafe_mode = this._was ?? false;
    }
}
