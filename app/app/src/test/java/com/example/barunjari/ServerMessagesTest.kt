package com.example.barunjari

import org.junit.Assert.*
import org.junit.Test

class ServerMessagesTest {
    @Test fun pressureFailureExplainsHowToRetry() {
        assertEquals("방석에 깊숙이 바르게 앉아 다시 측정해 주세요", baselineMessage("failed", "pressure_low"))
        assertTrue(baselineMessage("failed", "sensor_lost").contains("센서"))
        assertTrue(baselineMessage("waiting_seat", "").contains("앉아"))
    }
    @Test fun unknownAndFutureStatesNeverAppearAsNormal() {
        assertEquals("판정 불가", postureName("unknown"))
        assertEquals("판정 불가", postureName("future_state"))
        assertEquals("정상 자세", postureName("normal"))
    }
    @Test fun allBaselineStatesHaveDifferentMessages() {
        assertEquals(5, listOf("waiting_seat", "measuring", "ready", "failed", "none")
            .map { baselineMessage(it, "no_person") }.toSet().size)
    }
}
