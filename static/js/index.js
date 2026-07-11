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

    const sourceType = document.getElementById('source_type');
    const imagesFields = document.getElementById('images_fields');

    function syncSourceType() {
        if (!sourceType || !imagesFields) return;
        const isVideo = sourceType.value === 'video';

        imagesFields.style.display = isVideo ? 'none' : '';

        // disabledな入力欄はフォーム送信対象から除外されるため、videoモード選択時に
        // images専用欄(subs_url/whisper_model)の値が誤って送信されるのを防ぐ。
        imagesFields.querySelectorAll('input, select').forEach((el) => {
            el.disabled = isVideo;
        });
    }

    if (sourceType && imagesFields) {
        sourceType.addEventListener('change', syncSourceType);
        syncSourceType();
    }
});
