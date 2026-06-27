document.addEventListener('DOMContentLoaded', () => {
    const subsInput = document.getElementById('subs_url');
    const whisperField = document.getElementById('whisper_field');

    function sync() {
        whisperField.style.display = subsInput.value.trim() ? 'none' : '';
    }

    subsInput.addEventListener('input', sync);
    sync();
});
