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
    }

    if (sourceType && imagesFields && videoFields) {
        sourceType.addEventListener('change', syncSourceType);
        syncSourceType();
    }
});
