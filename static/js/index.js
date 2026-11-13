document.addEventListener('DOMContentLoaded', () => {
    const subsInput = document.getElementById('subs_url');
    const whisperField = document.getElementById('whisper_field');

    function syncWhisper() {
        if (!subsInput || !whisperField) return;
        whisperField.style.display = subsInput.value.trim() ? 'none' : '';
    }

    if (subsInput && whisperField) {
        subsInput.addEventListener('input', syncWhisper);
        syncWhisper();
    }

    const sourceTypeInput = document.getElementById('source_type');
    const sourceTypeTabs = document.querySelectorAll('#source_type_tabs [data-source-type]');
    const imagesPane = document.getElementById('images-pane');

    function syncSourceType(tabEl) {
        if (!sourceTypeInput || !imagesPane) return;
        const value = tabEl.dataset.sourceType;
        sourceTypeInput.value = value;

        // Disabled inputs are excluded from form submission, so this prevents the values of
        // the still-image-only fields (subs_url/whisper_model) from being sent by mistake
        // when the video tab is selected.
        imagesPane.querySelectorAll('input, select').forEach((el) => {
            el.disabled = value !== 'images';
        });
    }

    sourceTypeTabs.forEach((tabEl) => {
        tabEl.addEventListener('shown.bs.tab', () => syncSourceType(tabEl));
        if (tabEl.classList.contains('active')) {
            syncSourceType(tabEl);
        }
    });
});
