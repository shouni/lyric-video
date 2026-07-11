document.addEventListener('DOMContentLoaded', () => {
    const subsInput = document.getElementById('subs_url');
    const whisperField = document.getElementById('whisper_field');

    function syncWhisper() {
        whisperField.style.display = subsInput.value.trim() ? 'none' : '';
    }

    subsInput.addEventListener('input', syncWhisper);
    syncWhisper();

    const sourceType = document.getElementById('source_type');
    const imagesFields = document.getElementById('images_fields');
    const videoFields = document.getElementById('video_fields');

    function syncSourceType() {
        const isVideo = sourceType.value === 'video';
        imagesFields.style.display = isVideo ? 'none' : '';
        videoFields.style.display = isVideo ? '' : 'none';
    }

    if (sourceType) {
        sourceType.addEventListener('change', syncSourceType);
        syncSourceType();
    }
});
