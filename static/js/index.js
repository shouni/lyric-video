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
    const videoFields = document.getElementById('video_fields');

    function syncSourceType() {
        if (!sourceType || !imagesFields || !videoFields) return;
        const isVideo = sourceType.value === 'video';

        imagesFields.style.display = isVideo ? 'none' : '';
        videoFields.style.display = isVideo ? '' : 'none';

        // disabledな入力欄はフォーム送信対象から除外されるため、非表示側の値が
        // 誤って送信されるのを防ぐ（サーバー側バリデーションの混乱を避ける）。
        imagesFields.querySelectorAll('input, select').forEach((el) => {
            el.disabled = isVideo;
        });
        videoFields.querySelectorAll('input, select').forEach((el) => {
            el.disabled = !isVideo;
        });
    }

    if (sourceType && imagesFields && videoFields) {
        sourceType.addEventListener('change', syncSourceType);
        syncSourceType();
    }
});
