from pepper import config, logger

from webrtcvad import Vad
import numpy as np

from threading import Thread


class VAD(object):

    FRAME_MS = 10  # Must be either 10/20/30 ms, according to webrtcvad specification
    BUFFER_SIZE = 100  # Buffer Size
    WINDOW_SIZE = config.VAD_WINDOW_SIZE * FRAME_MS  # Sliding Window Length (Multiples of Frame MS)

    def __init__(self, microphone, callbacks, mode=3):
        """
        Detect Utterances of People using Voice Activity Detection

        Parameters
        ----------
        microphone: AbstractMicrophone
            Microphone to extract Utterances from
        callbacks: list of callable
            On Utterance Callback
        mode: int
            Voice Activity Detection (VAD) 'Aggressiveness' (1..3)
        """
        self._microphone = microphone
        self._microphone.callbacks += [self._on_audio]
        self._rate = microphone.rate

        self._callbacks = callbacks
        self._vad = Vad(mode)

        # Number of Elements (np.int16) in Frame
        self._frame_size = self.FRAME_MS * self.rate // 1000

        self._ringbuffer_index = 0

        self._activation = 0

        # Initialize Ringbuffers, which will hold Audio data and Vad.is_speech results, respectively
        self._audio_ringbuffer = np.zeros((self.BUFFER_SIZE, self._frame_size), np.int16)
        self._vad_ringbuffer = np.zeros(self.BUFFER_SIZE, np.bool)

        self._audio_buffer = bytearray()  # Audio Buffer will be filled with raw Microphone Audio
        self._voice_buffer = bytearray()  # Voice Buffer will be filled with Voiced Audio

        self._voice = False  # No Voice is present at start

        self._log = logger.getChild(self.__class__.__name__)
        self._log.debug("Booted")

    @property
    def microphone(self):
        """
        Returns
        -------
        microphone: Microphone
            Microphone to extract Utterances from
        """
        return self._microphone

    @property
    def callbacks(self):
        """
        Returns
        -------
        callback: list of callable
            On Utterance Callback
        """
        return self._callbacks

    @property
    def rate(self):
        """
        Returns
        -------
        sample_rate: int
            Microphone Sample Rate
        """
        return self._rate

    @property
    def vad(self):
        """
        Returns
        -------
        vad: Vad
            Voice Activity Detection Class
        """
        return self._vad

    def start(self):
        """Start Detecting Utterances"""
        self.microphone.start()

    def stop(self):
        """Stop Detecting Utterances"""
        self.microphone.stop()

    def on_utterance(self, audio):
        """
        On Utterance Callback, user specified callback(s) should have same signature

        Parameters
        ----------
        audio: np.ndarray
            Audio containing utterance
        """
        self._log.debug("Utterance {:3.2f}s".format(len(audio) / float(self.rate)))

        for callback in self.callbacks:
            callback(audio)

    @property
    def activation(self):
        """
        Returns
        -------
        activation: float
            Voice Activation Level [0,1]
        """
        return self._activation

    def _on_audio(self, audio):
        """
        Microphone On Audio Event, Processes Audio to filter out Utterances

        Parameters
        ----------
        audio: np.ndarray
        """

        # Put Microphone Audio at the end of the Audio Buffer
        self._audio_buffer.extend(audio.tobytes())

        # Process Each Frame-Length of Audio in Buffer
        # 2 * self._frame_size, because we're counting bytes, not np.int16's
        while len(self._audio_buffer) > 2 * self._frame_size:
            frame = np.frombuffer(self._audio_buffer[:2*self._frame_size], np.int16)
            self._process_frame(frame)
            self._process_voice(frame)
            del self._audio_buffer[:2*self._frame_size]

    def _process_frame(self, frame):
        """
        Process Single Frame of Audio, must be of length self._frame_size and of dtype np.int16

        Parameters
        ----------
        frame: np.ndarray
        """

        # Put Frame on Audio Ringbuffer
        self._audio_ringbuffer[self._ringbuffer_index] = frame

        # Check if Frame contains speech and put result on VAD Ringbuffer
        self._vad_ringbuffer[self._ringbuffer_index] = self.vad.is_speech(frame.tobytes(), self.rate, len(frame))

        # Update Ringbuffer Index
        self._ringbuffer_index = (self._ringbuffer_index + 1) % self.BUFFER_SIZE

    def _process_voice(self, frame):
        """
        Check if Utterance is currently starting/happening/stopping and act accordingly

        Parameters
        ----------
        frame: np.ndarray
        """
        window = np.arange(self._ringbuffer_index - self.WINDOW_SIZE, self._ringbuffer_index) % self.BUFFER_SIZE
        self._activation = np.mean(self._vad_ringbuffer[window])

        if self._voice:
            if self.activation > config.VAD_NONVOICE_THRESHOLD:
                self._voice_buffer.extend(frame)  # Add Frame to Voice Buffer
            else:
                self._voice = False  # Stop Recording Voice

                # Append Last Frame a couple of times, to give Google some room to play
                # TODO: Update this to some better solution, e.g. just listen a while longer
                for i in range(self.WINDOW_SIZE//2):
                    self._voice_buffer.extend(frame)

                # Cast Voice Buffer to Numpy Array
                result = np.frombuffer(self._voice_buffer, np.int16)

                # Call Utterance Callback in Thread, to prevent blocking
                Thread(target=self.on_utterance, args=(result,)).start()

                self._voice_buffer = bytearray()  # Clear Voice Buffer
        else:
            if self.activation > config.VAD_VOICE_THRESHOLD:
                self._voice = True  # Start Recording Voice

                # Add Buffered Audio to Voice Buffer
                self._voice_buffer.extend(self._audio_ringbuffer[self._ringbuffer_index:].tobytes())
                self._voice_buffer.extend(self._audio_ringbuffer[:self._ringbuffer_index].tobytes())
